#!/usr/bin/env python3
"""Check a secrets data repository against its registry. Never decrypts.

  secrets-check.py --report check.json --root DIR [--ssh-to-age CMD] [--scan PATH]

check.json is a data repository's report.check (lib.mkData):
  {sopsConfig, files: [{estate, file, path, keys, readers, operators}],
   allow: {missingKeys: ["path#key"], recipients: ["path"]}}

Checks (ADR-0009):
  sops-config  DIR/.sops.yaml is exactly what the registry renders.
  scan         no plaintext secret in any file (tools/hooks/secrets-scan.sh --all;
               a directory that is not a git work tree is scanned from a copy).
  keys         every file the registry names exists and holds every key it
               declares. Key names of a sops YAML, JSON or dotenv file are in
               clear; values are never read.
  recipients   every sops file a creation rule matches (first match, as sops
               does) is encrypted to every recipient of that rule; otherwise a
               reader cannot decrypt it. Recipients beyond the rule (stale) and
               sops files no rule matches are warnings.
allow excuses known drift: it is listed as excused, not failed. A file the
registry names that does not exist is excused when every key it declares is.

Human detail on stderr; stdout is one JSON line
  {"check": "pass"|"fail", "failures": N, "warnings": N, "excused": N}
and the exit code is 0 iff pass. Messages name files and keys, never values.
Stdlib only.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AGE = re.compile(r"recipient[^\n]*?(age1[02-9ac-hj-np-z]{58})")
# From fleet's tools/secrets_refs.py: a YAML mapping key at the start of a line.
KEY = re.compile(r"^(\s*)(\"[^\"]*\"|'[^']*'|[^\s#:\-\[\]{}\"'][^:#]*?|-[^\s:][^:#]*?)\s*:(?:\s|$)")


def load_renderer():
    spec = importlib.util.spec_from_file_location("sops_config", os.path.join(HERE, "sops-config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fmt(path):
    ext = os.path.splitext(path)[1].lower()
    return {".yaml": "yaml", ".yml": "yaml", ".json": "json", ".env": "dotenv"}.get(ext, "binary")


def yaml_paths(text):
    """Nested key paths ('a/b/c' and every prefix) of a sops YAML file, up to its sops: block."""
    out, stack = set(), []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = KEY.match(line)
        if not m:
            continue
        indent, name = len(m.group(1)), m.group(2).strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
            name = name[1:-1]
        if indent == 0 and name == "sops":
            break
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, name))
        out.add("/".join(n for _, n in stack))
    return out


def json_paths(text):
    try:
        d = json.loads(text)
    except ValueError:
        return set()
    out = set()

    def walk(v, pre):
        if isinstance(v, dict):
            for k, x in v.items():
                if not pre and k == "sops":
                    continue
                p = f"{pre}/{k}" if pre else k
                out.add(p)
                walk(x, p)

    walk(d, "")
    return out


def dotenv_paths(text):
    return {m[1] for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_.-]*)=", text, re.M) if not m[1].startswith("sops_")}


def is_sops(text, kind):
    if kind == "yaml":
        return re.search(r"^sops:", text, re.M) is not None
    if kind == "dotenv":
        return re.search(r"^sops_version=", text, re.M) is not None
    try:
        d = json.loads(text)
        return isinstance(d, dict) and isinstance(d.get("sops"), dict)
    except ValueError:
        return False


def repo_files(root):
    """Repository-relative paths of every file (git ls-files in a work tree, else a walk)."""
    if os.path.isdir(os.path.join(root, ".git")) or os.path.isfile(os.path.join(root, ".git")):
        r = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True)
        if r.returncode == 0:
            return sorted(p for p in r.stdout.decode().split("\0") if p and os.path.isfile(os.path.join(root, p)))
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def read(path):
    with open(path, errors="replace") as f:
        return f.read()


class Result:
    def __init__(self):
        self.failures, self.warnings, self.excused = [], [], []

    def fail(self, check, msg, excused=False):
        (self.excused if excused else self.failures).append(f"{check}: {msg}")

    def warn(self, check, msg):
        self.warnings.append(f"{check}: {msg}")


def check_sops_config(doc, root, cmd, res):
    r = load_renderer()
    path = os.path.join(root, ".sops.yaml")
    if not os.path.isfile(path):
        res.fail("sops-config", ".sops.yaml is missing; run `secrets render`")
        return None
    lines, keys, rules = r.build_config(doc, cmd)
    want = "\n".join(lines) + "\n"
    if read(path) != want:
        msgs = r.diff(r.parse(want), r.parse(read(path)))
        res.fail("sops-config", ".sops.yaml is not what the registry renders"
                 + (": " + "; ".join(msgs) if msgs else " (comments or layout differ)") + "; run `secrets render`")
    return keys, rules


def check_scan(root, scan, res):
    if not os.path.isfile(scan):
        res.fail("scan", f"scanner {scan} not found")
        return
    work, tmp = root, None
    if not (os.path.exists(os.path.join(root, ".git"))):
        tmp = tempfile.mkdtemp(prefix="secrets-check-")
        work = os.path.join(tmp, "repo")
        shutil.copytree(root, work, symlinks=True)
        for d, _, fs in os.walk(work):
            os.chmod(d, 0o755)
            for f in fs:
                p = os.path.join(d, f)
                if not os.path.islink(p):
                    os.chmod(p, 0o644)
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        for argv in (["git", "init", "-q", "."], ["git", "add", "-A"]):
            r = subprocess.run(argv, cwd=work, capture_output=True, env=env)
            if r.returncode != 0:
                res.fail("scan", f"could not prepare a copy to scan: {r.stderr.decode().strip()[-200:]}")
                shutil.rmtree(tmp, ignore_errors=True)
                return
    try:
        r = subprocess.run(["bash", scan, "--all"], cwd=work, capture_output=True, text=True)
        for line in r.stderr.splitlines():
            if line.strip():
                res.fail("scan", line.replace("secrets-scan: ", ""))
        if r.returncode != 0 and not r.stderr.strip():
            res.fail("scan", f"scanner exited {r.returncode}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def check_keys(files, root, allow, res):
    cache = {}
    for f in files:
        path = f["path"]
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            # A missing file is excused only when every key it declares is.
            res.fail("keys", f"{f['estate']}/{f['file']}: {path} does not exist",
                     bool(f["keys"]) and all(f"{path}#{k}" in allow for k in f["keys"]))
            continue
        kind = fmt(path)
        if kind == "binary":
            res.warn("keys", f"{path}: binary sops file, its keys are not checked")
            continue
        if path not in cache:
            text = read(full)
            cache[path] = {"yaml": yaml_paths, "json": json_paths, "dotenv": dotenv_paths}[kind](text)
        for k in f["keys"]:
            if k not in cache[path]:
                res.fail("keys", f"{path}: declared key {k} is not in the file", f"{path}#{k}" in allow)


def check_recipients(rules, keys, root, allow, protect, res):
    compiled = [(re.compile(rx), rx, names) for rx, names in rules]
    for path in repo_files(root):
        if path == ".sops.yaml" or path.startswith(".git/"):
            continue
        full = os.path.join(root, path)
        try:
            text = read(full)
        except OSError:
            continue
        if not is_sops(text, fmt(path) if fmt(path) != "binary" else "json"):
            continue
        rule = next(((rx, names) for c, rx, names in compiled if c.search(path)), None)
        if rule is None:
            res.warn("recipients", f"{path}: a sops file no creation rule matches (sops cannot re-key it)")
            continue
        want = {keys[n]: n for n in rule[1] if n in keys}
        have = set(AGE.findall(text))
        missing = sorted(n for a, n in want.items() if a not in have)
        stale = sorted(have - set(want))
        if missing:
            res.fail("recipients", f"{path}: not encrypted to {', '.join(missing)} (rule {rule[0]}); "
                     "those readers cannot decrypt it until `secrets rekey` runs", path in allow)
        if stale:
            res.warn("recipients", f"{path}: {len(stale)} recipient(s) not in its rule (stale)")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--report", required=True, help="report.check JSON file, or - for stdin")
    p.add_argument("--root", required=True, help="the data repository (a checkout or its store path)")
    p.add_argument("--ssh-to-age", default="ssh-to-age", metavar="COMMAND")
    p.add_argument("--scan", default=os.path.join(HERE, "hooks", "secrets-scan.sh"), metavar="PATH")
    p.add_argument("--skip", action="append", default=[], choices=["sops-config", "scan", "keys", "recipients"])
    a = p.parse_args()
    try:
        rep = json.load(sys.stdin if a.report == "-" else open(a.report))
    except (OSError, ValueError) as e:
        print(f"secrets-check: cannot read report: {e}", file=sys.stderr)
        sys.exit(2)
    root = os.path.abspath(a.root)
    allow = rep.get("allow") or {}
    res = Result()
    rendered = None
    if "sops-config" not in a.skip:
        rendered = check_sops_config(rep["sopsConfig"], root, a.ssh_to_age, res)
    if "scan" not in a.skip:
        check_scan(root, a.scan, res)
    if "keys" not in a.skip:
        check_keys(rep.get("files", []), root, set(allow.get("missingKeys", [])), res)
    if "recipients" not in a.skip:
        if rendered is None:
            r = load_renderer()
            _, k, ru = r.build_config(rep["sopsConfig"], a.ssh_to_age)
            rendered = (k, ru)
        check_recipients(rendered[1], rendered[0], root, set(allow.get("recipients", [])),
                         rep["sopsConfig"].get("protect", []), res)
    for m in res.failures:
        print(f"FAIL {m}", file=sys.stderr)
    for m in res.excused:
        print(f"excused {m}", file=sys.stderr)
    for m in res.warnings:
        print(f"warn {m}", file=sys.stderr)
    ok = not res.failures
    print(json.dumps({"check": "pass" if ok else "fail", "failures": len(res.failures),
                      "warnings": len(res.warnings), "excused": len(res.excused)}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
