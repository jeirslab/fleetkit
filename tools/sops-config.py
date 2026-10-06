#!/usr/bin/env python3
"""Render (or check) a .sops.yaml from one estate's fleet.report.sops object.

  nix eval --json .#fleet.report.sops.<estate> | tools/sops-config.py --report - [--check .sops.yaml]

Public key material only; this tool never decrypts anything. Stdlib only.
Report shape: {anchors: {name: {kind, ssh, age, id}}, rules: [{path, anchors}],
missingKeys: [...]}. An anchor with only an ssh key is converted with
`ssh-to-age` (override with --ssh-to-age).
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


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--report", required=True, help="report JSON file, or - for stdin")
    p.add_argument("--check", metavar=".sops.yaml", help="compare against this file; exit 1 on differences")
    p.add_argument("--ssh-to-age", default="ssh-to-age", metavar="COMMAND")
    a = p.parse_args()
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
