#!/usr/bin/env python3
"""Check a rendered Pulumi YAML program against its Terraform render and the
pinned Pulumi schemas.

  pulumi.py PROGRAM.json TF.json

Fails (exit 1, problems on stderr) when:
  - a Terraform resource has no Pulumi resource with the same logical name and
    the token the bridge gives its type, or the program has a resource the
    render does not;
  - a property (recursively) is not in the pinned Pulumi schema of its token,
    or is a list where the schema takes an object or the other way round;
    checked against providers/pulumi/schemas directly, not the name maps, so a
    wrong name map is caught too;
  - a ${...} names a resource or variable the program does not have;
  - a packages entry is not the terraform-provider bridge at the provider and
    version the Terraform render pins;
  - a provider credential is a literal instead of a sops invoke reference, or
    a lifecycle meta-argument is lost (prevent_destroy -> protect).
Evaluation only; no network.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "providers/pulumi/schemas"
NAMES = ROOT / "providers/pulumi/names"
REF = re.compile(r"\$\{([A-Za-z0-9_-]+)")
CRED = {"apiToken", "token", "pemFile", "id", "installationId"}

prog = json.load(open(sys.argv[1]))
tf = json.load(open(sys.argv[2]))
bad = []

pschemas = {}
for f in SCHEMAS.glob("*.pulumi.json"):
    d = json.loads(f.read_text())
    pschemas[d["name"]] = d
names = {json.loads(f.read_text())["source"]: json.loads(f.read_text()) for f in NAMES.glob("*.json")}


def schema_of(token):
    pkg = token.split(":")[0] if not token.startswith("pulumi:providers:") else token.split(":")[2]
    return pschemas.get(pkg)


def check_props(where, value, props, types):
    if not isinstance(value, dict):
        bad.append(f"{where}: an object is expected")
        return
    for k, v in value.items():
        if k not in props:
            bad.append(f"{where}.{k}: not in the Pulumi schema")
            continue
        check_value(f"{where}.{k}", v, props[k], types)


def check_value(where, v, prop, types):
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        return  # an output; its type is the referenced property's
    t = prop.get("type")
    if t == "array":
        if not isinstance(v, list):
            bad.append(f"{where}: the schema takes a list")
            return
        for i, x in enumerate(v):
            check_value(f"{where}[{i}]", x, prop.get("items", {}), types)
        return
    if t == "object" and "additionalProperties" in prop:
        if isinstance(v, dict):
            for k, x in v.items():
                check_value(f"{where}.{k}", x, prop["additionalProperties"], types)
        return
    ref = prop.get("$ref", "")
    if ref.startswith("#/types/"):
        if isinstance(v, list):
            bad.append(f"{where}: the schema takes an object, the program has a list")
            return
        check_props(where, v, types[ref[len("#/types/"):]].get("properties", {}), types)


# Resources: parity with the Terraform render, and the schema.
res = prog.get("resources", {})
seen = set()
for key, r in res.items():
    tok = r["type"]
    s = schema_of(tok)
    if s is None:
        bad.append(f"resources.{key}: no pinned Pulumi schema for {tok}")
        continue
    if tok.startswith("pulumi:providers:"):
        props = {**s.get("config", {}).get("variables", {}), **s.get("provider", {}).get("inputProperties", {})}
    elif tok not in s["resources"]:
        bad.append(f"resources.{key}: {tok} is not a resource of {s['name']}")
        continue
    else:
        props = s["resources"][tok].get("inputProperties", {})
        seen.add((tok, r.get("name", key)))
    check_props(f"resources.{key}", r.get("properties", {}), props, s.get("types", {}))

want = set()
pins = (tf.get("terraform") or {}).get("required_providers") or {}
for rtype, rs in (tf.get("resource") or {}).items():
    m = names[pins[rtype.split("_")[0]]["source"]]
    for name, body in rs.items():
        tok = m["resources"][rtype]["token"]
        want.add((tok, name))
        hits = [r for k, r in res.items() if r["type"] == tok and r.get("name", k) == name]
        if hits and (body.get("lifecycle") or {}).get("prevent_destroy"):
            if not (hits[0].get("options") or {}).get("protect"):
                bad.append(f"{rtype}.{name}: prevent_destroy is not options.protect")
for tok, name in sorted(want - seen):
    bad.append(f"missing: {tok} {name}")
for tok, name in sorted(seen - want):
    bad.append(f"extra: {tok} {name}")

# Packages: the bridge, at the pinned provider and version.
for local, rp in pins.items():
    m = names.get(rp["source"])
    pk = (prog.get("packages") or {}).get(m["package"] if m else "")
    if not m or not pk:
        bad.append(f"packages: no entry for {rp['source']}")
    elif pk.get("source") != "terraform-provider" or pk.get("parameters") != [rp["source"], rp["version"]]:
        bad.append(f"packages.{m['package']}: not the bridge at {rp['source']} {rp['version']}")

# Variables: invokes of pinned functions.
variables = prog.get("variables", {})
for key, v in variables.items():
    inv = v.get("fn::invoke", {})
    s = schema_of(inv.get("function", ""))
    if s is None or inv.get("function") not in s.get("functions", {}):
        bad.append(f"variables.{key}: {inv.get('function')} is not a pinned function")
        continue
    fn = s["functions"][inv["function"]]
    check_props(f"variables.{key}", inv.get("arguments", {}), fn["inputs"].get("properties", {}), s.get("types", {}))

# References resolve.
keys = set(res) | set(variables)
for m in REF.finditer(json.dumps(prog)):
    if m.group(1) not in keys:
        bad.append(f"reference ${{{m.group(1)}...}}: no such resource or variable")


# Credentials on providers are invoke references.
def walk(n, path):
    if isinstance(n, dict):
        for k, v in n.items():
            walk(v, path + [k])
    elif isinstance(n, list):
        for i, v in enumerate(n):
            walk(v, path + [str(i)])
    elif path[-1] in CRED and not (isinstance(n, str) and re.fullmatch(r"\$\{[A-Za-z0-9_-]+\.data\[\".+\"\]\}", n)
                                   and n[2:].split(".")[0] in variables):
        bad.append(f"literal credential at {'.'.join(path)}")


for key, r in res.items():
    if r["type"].startswith("pulumi:providers:"):
        walk(r.get("properties", {}), ["resources", key])

for b in bad:
    print(f"pulumi: {sys.argv[1]}: {b}", file=sys.stderr)
print(f"pulumi: {prog.get('name')}: {len(seen)} resources, {len(variables)} invokes, "
      f"{'ok' if not bad else str(len(bad)) + ' problem(s)'}", file=sys.stderr)
sys.exit(1 if bad else 0)
