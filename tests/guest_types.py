#!/usr/bin/env python3
"""Gate: the runner's guest list covers the pinned provider.

Every resource token of providers/pulumi/names/bpg-proxmox-*.json whose type
name matches vm / container / lxc must be in GUEST_TYPES or in NOT_GUESTS
(with a reason) of cli/fleetkit_cli/guests.py, and every token listed there
must exist in the provider. A provider bump that adds a guest type fails here
instead of leaving the guard open for it. LOCAL_PROPERTIES (what `fleetkit
adopt` applies without an acceptance) is, for each of its two guest types,
exactly the `timeout*` properties the pinned provider has for that type: a
bump that adds or renames one fails here until it is decided. Offline; exit 0
iff it holds.
(cli/tests/test_guests.py is the same check inside the package build.)"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ns: dict = {}
exec((ROOT / "cli/fleetkit_cli/guests.py").read_text(), ns)  # data only, no imports
GUESTS, NOT, PATTERN = ns["GUEST_TYPES"], ns["NOT_GUESTS"], ns["GUEST_PATTERN"]

files = sorted((ROOT / "providers/pulumi/names").glob("bpg-proxmox-*.json"))
if not files:
    sys.exit("guest_types: no providers/pulumi/names/bpg-proxmox-*.json")
tokens = sorted({r["token"] for f in files for r in json.loads(f.read_text())["resources"].values()
                 if re.search(PATTERN, r["token"].rsplit(":", 1)[-1], re.I)})
bad = [f"undecided: {t} is neither in GUEST_TYPES nor in NOT_GUESTS" for t in tokens if t not in GUESTS and t not in NOT]
bad += [f"stale: {t} is not a resource of the pinned provider" for t in sorted({*GUESTS, *NOT} - set(tokens))]
bad += [f"both: {t}" for t in sorted(set(GUESTS) & set(NOT))]
LOCAL = ns["LOCAL_PROPERTIES"]
by_token = {r["token"]: r for f in files for r in json.loads(f.read_text())["resources"].values()}
for token, props in sorted(LOCAL.items()):
    if token not in GUESTS or token not in by_token:
        bad.append(f"local: {token} is not a guest type of the pinned provider")
        continue
    have = {v["n"] for k, v in by_token[token]["f"].items() if k.startswith("timeout")}
    if have != set(props):
        bad.append(f"local: {token}: the provider's timeout properties are {sorted(have)}, "
                   f"LOCAL_PROPERTIES has {sorted(props)}")
for line in bad:
    print(f"guest_types: FAIL {line}", file=sys.stderr)
print(json.dumps({"guest_types": "fail" if bad else "pass", "tokens": len(tokens), "guests": len(GUESTS)}))
sys.exit(1 if bad else 0)
