#!/usr/bin/env python3
"""Check a rendered main.tf.json against the pinned bpg/proxmox schema.

  terraform.py RENDERED.json [--schema FILE]

Fails (exit 1, problems on stderr) when:
  - a resource type is not in the schema, or an argument / nested block name
    (recursively) is not an attribute or block of that resource;
  - the managed guests (lxc and vm) or the pool are not at their expected
    addresses, or the adopted guest is rendered / not in locals.fleet_unmanaged;
  - any api_token is a literal instead of a ${data.sops_file...} reference,
    the data.sops_file it references is not rendered, or it is not the
    estate's placement.tokenRef (file alias, path and key);
  - the guest with an lxc_extra_conf companion is not listed in
    locals.fleet_unrendered_companions.
Evaluation only; no network.
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = f"{ROOT}/providers/schemas/bpg-proxmox-0.115.0.schema.json"
PROVIDER = "registry.opentofu.org/bpg/proxmox"
LXC = "proxmox_virtual_environment_container"
VM = "proxmox_virtual_environment_vm"
POOL = "proxmox_virtual_environment_pool"
META = {"lifecycle", "depends_on", "count", "for_each", "provider", "provisioner"}
SOPS_REF = re.compile(r'^\$\{data\.sops_file\.([A-Za-z0-9_-]+)\.data\["(.+)"\]\}$')

# The fixture (tests/fixtures/tf-mini): expected addresses.
MANAGED = {LXC: "box", VM: "machine"}
POOL_NAME = "main"
ADOPTED = "legacy"
COMPANION = "tuned"
# placement.tokenRef is sops:mini/tf#pve-token; the site provider's own
# tokenRef names another key, which must not be the one rendered.
TOKEN_FILE = "tf"
TOKEN_KEY = "pve-token"
TOKEN_PATH = "secrets/tf.json"


def object_members(typ):
    """Keys of an attribute type that is (a collection of) an object, else None."""
    if isinstance(typ, list) and len(typ) == 2:
        if typ[0] == "object" and isinstance(typ[1], dict):
            return typ[1]
        if typ[0] in ("list", "set", "map"):
            return object_members(typ[1])
    return None


def check_attr_value(value, typ, where, problems):
    """Keys inside an object-typed attribute must be members of that object."""
    members = object_members(typ)
    if members is None:
        return
    items = value if isinstance(value, list) else [value]
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            if k not in members:
                problems.append(f"{where}[{i}]: '{k}' is not a member of the attribute type")
            else:
                check_attr_value(v, members[k], f"{where}[{i}].{k}", problems)


def check_block(value, block, where, problems):
    attrs = block.get("attributes", {})
    blocks = block.get("block_types", {})
    for key, v in value.items():
        if key in attrs:
            check_attr_value(v, attrs[key].get("type"), f"{where}.{key}", problems)
        elif key in blocks:
            sub = blocks[key]["block"]
            items = v if isinstance(v, list) else [v]
            for i, item in enumerate(items):
                if isinstance(item, dict):
                    check_block(item, sub, f"{where}.{key}[{i}]", problems)
                else:
                    problems.append(f"{where}.{key}[{i}]: block must be an object")
        else:
            problems.append(f"{where}: '{key}' is not an attribute or block")


def walk_strings(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_strings(v, f"{path}[{i}]")
    else:
        yield path, node


def check(doc, schemas):
    problems = []
    resources = doc.get("resource", {})

    for rtype, instances in resources.items():
        res = schemas.get(rtype)
        if res is None:
            problems.append(f"resource type '{rtype}' is not in the bpg/proxmox schema")
            continue
        for name, body in instances.items():
            where = f"{rtype}.{name}"
            args = {k: v for k, v in body.items() if k not in META}
            check_block(args, res["block"], where, problems)
            lc = body.get("lifecycle")
            if lc is not None and (not isinstance(lc, dict) or not lc):
                problems.append(f"{where}: empty or malformed lifecycle must be dropped")

    for rtype, name in MANAGED.items():
        if name not in resources.get(rtype, {}):
            problems.append(f"missing managed guest at {rtype}.{name}")
    if POOL_NAME not in resources.get(POOL, {}):
        problems.append(f"missing pool at {POOL}.{POOL_NAME}")
    elif resources[POOL][POOL_NAME].get("pool_id") != POOL_NAME:
        problems.append(f"{POOL}.{POOL_NAME}: pool_id must be '{POOL_NAME}'")

    for rtype, instances in resources.items():
        if ADOPTED in instances:
            problems.append(f"adopted guest rendered at {rtype}.{ADOPTED}")
    unmanaged = doc.get("locals", {}).get("fleet_unmanaged")
    if not isinstance(unmanaged, (list, dict)) or not any(
        ADOPTED in str(u) for u in (unmanaged if isinstance(unmanaged, list) else list(unmanaged))
    ):
        problems.append(f"locals.fleet_unmanaged does not list the adopted guest '{ADOPTED}'")

    companions = doc.get("locals", {}).get("fleet_unrendered_companions")
    if not isinstance(companions, list) or COMPANION not in companions:
        problems.append(
            f"locals.fleet_unrendered_companions does not list the guest '{COMPANION}'"
        )

    sops_files = doc.get("data", {}).get("sops_file", {})
    tokens = 0
    for path, val in walk_strings(doc):
        if path.endswith(".api_token"):
            tokens += 1
            m = SOPS_REF.match(val) if isinstance(val, str) else None
            if not m:
                problems.append(f"{path}: api_token is not a ${{data.sops_file...}} reference")
                continue
            name, key = m.groups()
            if name not in sops_files:
                problems.append(f"{path}: data.sops_file.{name} is not rendered")
            elif sops_files[name].get("source_file") != TOKEN_PATH:
                problems.append(
                    f"data.sops_file.{name}: source_file is not the placement file '{TOKEN_PATH}'"
                )
            if name != TOKEN_FILE:
                problems.append(
                    f"{path}: references data.sops_file.{name}, not the file alias '{TOKEN_FILE}'"
                )
            if key != TOKEN_KEY:
                problems.append(f"{path}: key '{key}' is not the placement key '{TOKEN_KEY}'")
    if tokens == 0:
        problems.append("no provider api_token rendered")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rendered")
    ap.add_argument("--schema", default=SCHEMA)
    a = ap.parse_args()
    with open(a.schema, encoding="utf-8") as f:
        schemas = json.load(f)[PROVIDER]["resource_schemas"]
    with open(a.rendered, encoding="utf-8") as f:
        doc = json.load(f)
    problems = check(doc, schemas)
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
