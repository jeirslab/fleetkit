#!/usr/bin/env python3
"""Gate: the runner's type lists cover the pinned provider.

Every resource token of providers/pulumi/names/bpg-proxmox-*.json must be in
exactly one of GUEST_TYPES, HA_TYPES, NOT_GUESTS (with a reason) and
OTHER_TYPES of cli/fleetkit_cli/guests.py; a token whose type name matches
vm / container / lxc must be a guest or in NOT_GUESTS, never in OTHER_TYPES;
and every token listed there must exist in the provider. A provider bump that
adds a type fails here until it is decided, instead of leaving the guard open
for it (and the guard itself refuses a proxmox type it does not know).
LOCAL_PROPERTIES (what `fleetkit adopt` applies without an acceptance) is,
for each of its two guest types, exactly the `timeout*` properties the pinned
provider has for that type: a bump that adds or renames one fails here until
it is decided. Offline; exit 0 iff it holds. (cli/tests/test_guests.py is the
same check inside the package build.)

  guest_types.py [NAMES_DIR]    the directory of the name maps (default: the repo's)"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ns: dict = {}
exec((ROOT / "cli/fleetkit_cli/guests.py").read_text(), ns)  # data only, no imports
GUESTS, HA, NOT, OTHER, PATTERN = ns["GUEST_TYPES"], ns["HA_TYPES"], ns["NOT_GUESTS"], ns["OTHER_TYPES"], \
    ns["GUEST_PATTERN"]
SETS = {"GUEST_TYPES": set(GUESTS), "HA_TYPES": set(HA), "NOT_GUESTS": set(NOT), "OTHER_TYPES": set(OTHER)}

names = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "providers/pulumi/names"
files = sorted(names.glob("bpg-proxmox-*.json"))
if not files:
    sys.exit(f"guest_types: no bpg-proxmox-*.json in {names}")
every = sorted({r["token"] for f in files for r in json.loads(f.read_text())["resources"].values()})
tokens = [t for t in every if re.search(PATTERN, t.rsplit(":", 1)[-1], re.I)]
listed = set().union(*SETS.values())
bad = [f"undecided: {t} is neither in GUEST_TYPES nor in NOT_GUESTS" for t in tokens if t not in GUESTS and t not in NOT]
bad += [f"unknown: {t} is a resource of the pinned provider and in none of GUEST_TYPES, HA_TYPES, NOT_GUESTS, "
        f"OTHER_TYPES (the guard would refuse it; decide what it is)" for t in every if t not in listed]
bad += [f"stale: {t} is not a resource of the pinned provider" for t in sorted(listed - set(every))]
bad += [f"twice: {t} is in {a} and in {b}" for a in SETS for b in SETS if a < b for t in sorted(SETS[a] & SETS[b])]
bad += [f"no reason: {t}" for t, why in NOT.items() if len(why) <= 20]
if set(ns["GATED_TYPES"]) != set(GUESTS) | set(HA) or set(ns["KNOWN_TYPES"]) != listed:
    bad.append("GATED_TYPES / KNOWN_TYPES are not the unions of the lists")
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
print(json.dumps({"guest_types": "fail" if bad else "pass", "tokens": len(every), "guest_like": len(tokens),
                  "guests": len(GUESTS), "ha": len(HA), "other": len(OTHER) + len(NOT)}))
sys.exit(1 if bad else 0)
