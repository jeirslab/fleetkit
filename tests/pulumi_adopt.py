#!/usr/bin/env python3
"""Check the adoption ids of evaluated Pulumi.nix stacks (lib.pulumi.stacks).

  pulumi_adopt.py STACKS.json CASE [GITHUB_TYPES]

STACKS.json is { <stack>: { program, file, adoptIds, adoptUnresolved } }; CASE
is "" or "+<fixture>", the suffix of the stack's entry in
tests/fixtures/adopt-expected.json. Fails (exit 1, problems on stderr) when:
  - a stack's adoptIds or adoptUnresolved is not, key by key, what the expected
    file says (an id that changes is a different resource adopted);
  - a rendered program (the attrset, or the text of the store file) carries an
    `import` option, or a resource has a key other than type, name, properties
    and options (adoption data in the program);
  - a resource (providers aside) is in neither or in both of adoptIds and
    adoptUnresolved, or one of them names a resource the program does not have;
  - GITHUB_TYPES (comma-separated Terraform types) is given and one of them is
    not among the stack's resources: every type the kit renders has a rule.
Evaluation only; no network.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
expected = json.loads((ROOT / "tests/fixtures/adopt-expected.json").read_text())
stacks = json.load(open(sys.argv[1]))
case = sys.argv[2]
types = [t for t in (sys.argv[3] if len(sys.argv) > 3 else "").split(",") if t]
bad = []


def camel(tf_type):  # github_team_members -> github:index/teamMembers:TeamMembers
    parts = tf_type.split("_")[1:]
    low = parts[0] + "".join(p.title() for p in parts[1:])
    return f"github:index/{low}:{low[0].upper()}{low[1:]}"


def imports(n, path):  # every `import` key anywhere under a resource
    if isinstance(n, dict):
        for k, v in n.items():
            if k == "import" and path[-1:] != ["properties"]:
                yield ".".join(path + [k])
            yield from imports(v, path + [k])
    elif isinstance(n, list):
        for i, v in enumerate(n):
            yield from imports(v, path + [str(i)])


for name, s in sorted(stacks.items()):
    want = expected.get(name + case)
    if want is None:
        bad.append(f"{name}{case}: no entry in tests/fixtures/adopt-expected.json")
        continue
    for attr in ("adoptIds", "adoptUnresolved"):
        got, exp = s[attr], want[attr]
        for k in sorted(set(got) | set(exp)):
            if got.get(k) != exp.get(k):
                bad.append(f"{name}.{attr}.{k}: {got.get(k)!r}, expected {exp.get(k)!r}")

    res = s["program"]["resources"]
    for key, r in res.items():
        extra = set(r) - {"type", "name", "properties", "options"}
        if extra:
            bad.append(f"{name}: resources.{key} has {', '.join(sorted(extra))}")
        for where in imports(r, ["resources", key]):
            bad.append(f"{name}: {where}: an import in the program")
    text = pathlib.Path(s["file"]).read_text()
    if json.loads(text) != s["program"]:
        bad.append(f"{name}: file is not the program")
    if re.search(r'"(import|adopt[A-Za-z]*)"\s*:', text):
        bad.append(f"{name}: {s['file']} names import or adopt")

    managed = {k for k, r in res.items() if not r["type"].startswith("pulumi:providers:")}
    ids, unresolved = set(s["adoptIds"]), set(s["adoptUnresolved"])
    for k in sorted(managed - ids - unresolved):
        bad.append(f"{name}: resources.{k} ({res[k]['type']}) has neither an adoption id nor a reason")
    for k in sorted(ids & unresolved):
        bad.append(f"{name}: {k} is in adoptIds and in adoptUnresolved")
    for k in sorted((ids | unresolved) - managed):
        bad.append(f"{name}: {k} is not a resource of the program")
    have = {r["type"] for r in res.values()}
    for t in types:
        if camel(t) not in have:
            bad.append(f"{name}: no {t} ({camel(t)}) in the fixture: its adoption rule is not tested")
    print(f"pulumi_adopt: {name}{case}: {len(ids)} ids, {len(unresolved)} unresolved", file=sys.stderr)

for b in bad:
    print(f"pulumi_adopt: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
