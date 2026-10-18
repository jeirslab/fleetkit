#!/usr/bin/env python3
"""Render (or check) a .sops.yaml from one estate's fleet.report.sops object.

  nix eval --json .#fleet.report.sops.<estate> | tools/sops-config.py --report - [--check .sops.yaml]

Public key material only; this tool never decrypts anything. Stdlib only.
Report shape: {anchors: {name: {kind, ssh, age, id}}, rules: [{path, anchors}],
missingKeys: [...]}. An anchor with only an ssh key is converted with
`ssh-to-age` (override with --ssh-to-age).

Multi-estate (ADR-0009): --config takes fleet.report.secrets.sopsConfig (a data
repository's report.sops) instead of --report:

  {header, estates: [{name, prefix, root, keysComment, rulesComment,
                      keys: [{name, age}], rules: [{pathRegex, anchors}],
                      model: <one estate's report, as above>}]}

It writes the header ('# ' per line), then `keys:` with each estate's derived
anchors (sorted, as --report does) followed by its verbatim keys (in order),
then `creation_rules:` with each estate's derived rules (sorted) followed by its
verbatim rules (in order, re-rooted under root). Every anchor name carries the
estate's prefix. --check compares the text: the file must be exactly what is
rendered (anchors or rules that differ are named; otherwise a unified diff).

--import FILE reads a hand-written .sops.yaml (anchored age keys, path_regex,
one age key group of aliases; trailing comments ignored) and prints its keys
and rules, in order, as the JSON of an estate's secrets.layout.{keys,rules}.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys


def die(msg, code=2):
    print(f"sops-config: {msg}", file=sys.stderr)
    sys.exit(code)


def age_for(name, anchor, cmd):
    if anchor.get("age"):
        return anchor["age"]
    ssh = anchor.get("ssh")
    if not ssh:
        return None
    if shutil.which(cmd) is None:
        die(f"anchor '{name}' has only an ssh key and '{cmd}' was not found; "
            "install ssh-to-age or pass --ssh-to-age <command>")
    r = subprocess.run([cmd], input=ssh + "\n", capture_output=True, text=True)
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        die(f"'{cmd}' failed for anchor '{name}': {r.stderr.strip()}")
    return out


def build(report, cmd):
    """Return (keys: {name: age}, rules: [(path_regex, [names])])."""
    keys = {}
    for name in sorted(report.get("anchors", {})):
        age = age_for(name, report["anchors"][name], cmd)
        if age is None:
            print(f"sops-config: warning: anchor '{name}' has no key, omitted",
                  file=sys.stderr)
        else:
            keys[name] = age
    rules = []
    for rule in sorted(report.get("rules", []), key=lambda r: r["path"]):
        names = sorted({n for n in rule["anchors"] if n in keys})
        if not names:
            print(f"sops-config: warning: rule '{rule['path']}' has no usable "
                  "anchor, omitted", file=sys.stderr)
            continue
        rules.append(("^" + re.escape(rule["path"]) + "$", names))
    return keys, rules


def render(keys, rules):
    out = ["keys:"]
    out += [f"  - &{n} {a}" for n, a in keys.items()]
    out.append("creation_rules:")
    for rx, names in rules:
        out += [f"  - path_regex: {rx}", "    key_groups:", "      - age:"]
        out += [f"          - *{n}" for n in names]
    return "\n".join(out) + "\n"


def parse(text):
    """Parse the subset render() emits back into (keys, rules)."""
    keys, rules = {}, []
    section = None
    for ln, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line == "keys:":
            section = "keys"
        elif line == "creation_rules:":
            section = "rules"
        elif section == "keys" and (m := re.fullmatch(r"  - &(\S+) (\S+)", line)):
            keys[m[1]] = m[2]
        elif section == "rules" and (m := re.fullmatch(r"  - path_regex: (.+)", line)):
            rules.append((m[1], []))
        elif section == "rules" and line in ("    key_groups:", "      - age:"):
            pass
        elif section == "rules" and rules and (m := re.fullmatch(r"          - \*(\S+)", line)):
            rules[-1][1].append(m[1])
        else:
            die(f"cannot parse line {ln} of the checked file: {line!r}")
    return keys, [(rx, sorted(ns)) for rx, ns in rules]


def diff(want, have):
    wk, wr = want
    hk, hr = have
    msgs = []
    for n in sorted(set(wk) | set(hk)):
        if n not in hk:
            msgs.append(f"anchor {n}: missing from file")
        elif n not in wk:
            msgs.append(f"anchor {n}: not in the model")
        elif wk[n] != hk[n]:
            msgs.append(f"anchor {n}: recipient differs (model {wk[n]}, file {hk[n]})")
    wd, hd = dict(wr), dict(hr)
    for rx in sorted(set(wd) | set(hd)):
        if rx not in hd:
            msgs.append(f"rule {rx}: missing from file")
        elif rx not in wd:
            msgs.append(f"rule {rx}: not in the model")
        elif wd[rx] != hd[rx]:
            msgs.append(f"rule {rx}: anchors differ (model {wd[rx]}, file {hd[rx]})")
    if [rx for rx, _ in wr if rx in hd] != [rx for rx, _ in hr if rx in wd]:
        msgs.append("rule order differs")
    return msgs


# ADR-0009: multi-estate .sops.yaml -------------------------------------------


def reroot(rx, root):
    """A verbatim path_regex under root: anchored (^x) -> ^root/x, unanchored -> ^root/.*x."""
    if not root:
        return rx
    base = "^" + re.escape(root.rstrip("/") + "/")
    return base + rx[1:] if rx.startswith("^") else base + ".*" + rx


def comment(text, indent):
    return [(indent + "# " + ln).rstrip() for ln in text.splitlines()] if text else []


def build_config(doc, cmd):
    """Return (lines, keys: {name: age}, rules: [(path_regex, [names])]) in output order."""
    keys, key_lines, rule_lines, rules = {}, [], [], []
    for est in doc.get("estates", []):
        pre = est.get("prefix") or ""
        dk, dr = build(est.get("model") or {}, cmd)
        own = dict(dk)
        key_lines += comment(est.get("keysComment"), "  ")
        for n, a in list(dk.items()) + [(k["name"], k["age"]) for k in est.get("keys", [])]:
            if pre + n in keys:
                die(f"estate {est.get('name')}: anchor '{pre + n}' is declared twice")
            own[n] = a
            keys[pre + n] = a
            key_lines.append(f"  - &{pre + n} {a}")
        rule_lines += comment(est.get("rulesComment"), "  ")
        verbatim = []
        for r in est.get("rules", []):
            unknown = [n for n in r["anchors"] if n not in own]
            if unknown:
                die(f"estate {est.get('name')}: rule '{r['pathRegex']}' names unknown anchor(s) {', '.join(unknown)}")
            verbatim.append((reroot(r["pathRegex"], est.get("root") or ""), list(r["anchors"])))
        for rx, names in dr + verbatim:
            names = [pre + n for n in names]
            rules.append((rx, names))
            rule_lines += [f"  - path_regex: {rx}", "    key_groups:", "      - age:"]
            rule_lines += [f"          - *{n}" for n in names]
    lines = comment(doc.get("header"), "") + ["keys:"] + key_lines + ["creation_rules:"] + rule_lines
    return lines, keys, rules


def render_config(doc, cmd):
    return "\n".join(build_config(doc, cmd)[0]) + "\n"


def import_layout(path):
    """A hand-written .sops.yaml -> {keys: [{name, age}], rules: [{pathRegex, anchors}]}, in order."""
    keys, rules, section = [], [], None
    names = set()
    try:
        text = open(path).read()
    except OSError as e:
        die(f"cannot read {path}: {e}")
    for ln, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line in ("keys:", "creation_rules:"):
            section = line[:-1]
        elif section == "keys" and (m := re.fullmatch(r"  - &(\S+) (age1\S+)", line)):
            keys.append({"name": m[1], "age": m[2]})
            names.add(m[1])
        elif section == "creation_rules" and (m := re.fullmatch(r"  - path_regex: (\S+)", line)):
            rules.append({"pathRegex": m[1], "anchors": []})
        elif section == "creation_rules" and line.strip() in ("key_groups:", "- age:"):
            pass
        elif section == "creation_rules" and rules and (m := re.fullmatch(r"\s+- \*(\S+)", line)):
            if m[1] not in names:
                die(f"{path}:{ln}: unknown anchor {m[1]}")
            rules[-1]["anchors"].append(m[1])
        else:
            die(f"{path}:{ln}: cannot parse {raw!r}")
    return {"keys": keys, "rules": rules}


def check_config(doc, cmd, path):
    want = render_config(doc, cmd)
    try:
        have = open(path).read()
    except OSError as e:
        die(f"cannot read {path}: {e}")
    if want == have:
        print(f"{path}: ok", file=sys.stderr)
        return 0
    print(f"{path} differs from the registry:", file=sys.stderr)
    msgs = diff(parse(want), parse(have))
    for m in msgs:
        print(f"  {m}", file=sys.stderr)
    if not msgs:
        import difflib
        sys.stderr.writelines(difflib.unified_diff(have.splitlines(True), want.splitlines(True),
                                                   path, "rendered"))
    return 1


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--report", help="report JSON file, or - for stdin")
    src.add_argument("--config", help="multi-estate sopsConfig JSON file, or - for stdin (ADR-0009)")
    src.add_argument("--import", dest="import_", metavar=".sops.yaml",
                     help="print a hand-written .sops.yaml's keys and rules as layout JSON (ADR-0009)")
    p.add_argument("--check", metavar=".sops.yaml", help="compare against this file; exit 1 on differences")
    p.add_argument("--ssh-to-age", default="ssh-to-age", metavar="COMMAND")
    a = p.parse_args()
    if a.import_:
        json.dump(import_layout(a.import_), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    if a.config:
        try:
            doc = json.load(sys.stdin if a.config == "-" else open(a.config))
        except (OSError, ValueError) as e:
            die(f"cannot read config: {e}")
        if not a.check:
            sys.stdout.write(render_config(doc, a.ssh_to_age))
            return
        sys.exit(check_config(doc, a.ssh_to_age, a.check))
    try:
        report = json.load(sys.stdin if a.report == "-" else open(a.report))
    except (OSError, ValueError) as e:
        die(f"cannot read report: {e}")
    keys, rules = build(report, a.ssh_to_age)
    if not a.check:
        sys.stdout.write(render(keys, rules))
        return
    try:
        have = parse(open(a.check).read())
    except OSError as e:
        die(f"cannot read {a.check}: {e}")
    msgs = diff((keys, rules), have)
    if msgs:
        print(f"{a.check} differs from the model:", file=sys.stderr)
        for m in msgs:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)
    print(f"{a.check}: ok", file=sys.stderr)


if __name__ == "__main__":
    main()
