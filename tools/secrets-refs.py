#!/usr/bin/env python3
"""Every sops secret a system declares points at a file that exists and, for a
YAML, JSON or dotenv file, at a key that exists in it. Never decrypts.

  secrets-refs.py --flake REF [--prefix PREFIX] [SYSTEM ...]
  secrets-refs.py --refs refs.json

With --flake, each system's `config.sops.secrets` is evaluated (one `nix eval`
per system, so it is slow): SYSTEMs, or every nixosConfiguration whose name
starts with PREFIX (default: all). sopsFile is the absolute path sops-nix
has, usually a store path of the data repository; the file is read there. Key
names of a sops file are in clear; values are never read.

Generalised from fleet's tools/secrets-refs.sh and tools/secrets_refs.py
(ADR-0009). Human detail on stderr; stdout is one JSON line
  {"systems": N, "refs": N, "missing": [...], "pass": true|false}
exit 0 iff pass.
"""
import argparse
import json
import os
import re
import subprocess
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location(
    "secrets_check", os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets-check.py"))
_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_check)

APPLY = ('s: builtins.map (n: { name = n; sopsFile = toString s.${n}.sopsFile; key = s.${n}.key; '
         'format = s.${n}.format; }) (builtins.attrNames s)')


def shown(path):
    """A store path without its /nix/store/<hash>-<name>/ part, for messages."""
    m = re.match(r"^/nix/store/[^/]+/(.*)$", path)
    return m.group(1) if m else path


def nix_eval(args):
    return subprocess.run(["nix", "eval", "--json", *args], capture_output=True, text=True)


def collect(flake, prefix, systems):
    if not systems:
        r = nix_eval([f"{flake}#nixosConfigurations", "--apply", "builtins.attrNames"])
        if r.returncode != 0:
            print(r.stderr[-2000:], file=sys.stderr)
            sys.exit(2)
        systems = [n for n in json.loads(r.stdout) if n.startswith(prefix)]
    refs, failed = [], 0
    for s in systems:
        r = nix_eval([f"{flake}#nixosConfigurations.{s}.config.sops.secrets", "--apply", APPLY])
        if r.returncode != 0:
            failed += 1
            print(f"FAIL {s}: {' '.join(r.stderr.strip().splitlines()[-3:])}", file=sys.stderr)
            continue
        refs += [{"system": s, **x} for x in json.loads(r.stdout)]
        print(f"ok   {s}", file=sys.stderr)
    return systems, refs, failed


def check(refs):
    cache, missing = {}, []
    for r in refs:
        sf = r.get("sopsFile") or ""
        base = {"system": r.get("system"), "name": r.get("name"), "sopsFile": shown(sf)}
        if not sf or not os.path.isfile(sf):
            missing.append({**base, "reason": "file"})
            continue
        kind = r.get("format") or "yaml"
        if kind not in ("yaml", "json", "dotenv"):
            continue
        key = r.get("key") or r.get("name")
        if key == "":
            continue
        if sf not in cache:
            text = _check.read(sf)
            cache[sf] = {"yaml": _check.yaml_paths, "json": _check.json_paths,
                         "dotenv": _check.dotenv_paths}[kind](text)
        if key not in cache[sf]:
            missing.append({**base, "key": key, "reason": "key"})
    return missing


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--flake", help="a flake reference whose nixosConfigurations are checked")
    src.add_argument("--refs", help="a JSON list of {system, name, sopsFile, key, format}")
    p.add_argument("--prefix", default="", help="only systems whose name starts with this")
    p.add_argument("systems", nargs="*")
    a = p.parse_args()
    if a.refs:
        refs = json.load(open(a.refs))
        systems, failed = sorted({r.get("system") for r in refs}), 0
    else:
        systems, refs, failed = collect(a.flake, a.prefix, a.systems)
    missing = check(refs)
    for m in missing:
        print(f"missing {m['reason']}: {m['system']} {m['name']} -> {m['sopsFile']}"
              + (f"#{m['key']}" if "key" in m else ""), file=sys.stderr)
    ok = not missing and failed == 0 and len(systems) > 0
    print(json.dumps({"systems": len(systems), "refs": len(refs), "missing": missing, "pass": ok}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
