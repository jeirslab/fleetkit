#!/usr/bin/env python3
"""Check a rendered main.tf.json against the pinned bpg/proxmox schema.

  terraform.py RENDERED.json [--estate mini|tenant|bare|gaps] [--schema FILE]

Fails (exit 1, problems on stderr) when:
  - a resource type is not in the schema, or an argument / nested block name
    (recursively) is not an attribute or block of that resource, or a string,
    number or bool argument holds a value of another type;
  - the managed guests (lxc and vm) or the pool are not at their expected
    addresses, or the adopted guest is rendered / not in locals.fleet_unmanaged;
  - any api_token is a literal instead of a ${data.sops_file...} reference,
    the data.sops_file it references is not rendered, or it is not the
    expected one for the estate (file alias, path and key): mini its own
    placement.tokenRef, tenant and bare the provider's tokenRef, resolved
    through the secrets of the estate that owns it (mini);
  - the guest with an lxc_extra_conf companion is not listed in
    locals.fleet_unrendered_companions, or does not ignore its description;
  - for the estate "gaps" (every option added for adopting existing guests):
    an argument is not exactly the expected value, or one that must be absent
    (the console block and pool_id of the guest that opts out of both, the
    defaults of the others) is rendered (EXPECT, ABSENT).
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

# The fixture (tests/fixtures/tf-mini): per estate, the expected addresses and
# credential. mini's placement.tokenRef is sops:mini/tf#integrations/proxmox/main/api_token; the site
# provider's tokenRef is sops:mini/tf#site-token, in mini's secrets. tenant and
# bare have no placement.tokenRef, so they render the provider's, and since
# the file is mini's the data.sops_file is keyed mini_tf, not tf.
EP = "https://192.0.2.10:8006/"  # site s1, where every estate's guests are
ESTATES = {
    "mini": dict(
        managed={LXC: "box", VM: "machine"},
        pool="main",
        adopted="legacy",
        companion="tuned",
        file="tf",
        key="integrations.proxmox.main.api_token",
        endpoint=EP,
    ),
    "tenant": dict(
        managed={LXC: "tbox"},
        pool="tpool",
        adopted=None,
        companion=None,
        file="mini_tf",
        key="site-token",
        endpoint=EP,
    ),
    "bare": dict(
        managed={LXC: "bbox"},
        pool=None,
        adopted=None,
        companion=None,
        file="mini_tf",
        key="site-token",
        endpoint=EP,
    ),
    "gaps": dict(
        managed={LXC: "mapped", VM: "efi"},
        pool="gpool",
        adopted=None,
        companion="conf",
        file="mini_tf",
        key="site-token",
        endpoint=EP,
    ),
}
IDMAP = [
    {"type": t, "container_id": c, "host_id": h, "size": n}
    for t in ("uid", "gid")
    for c, h, n in ((0, 100000, 1000), (1000, 1000, 1), (1001, 101001, 64535))
]
# Estate gaps: (resource type, name) -> argument -> the exact rendered value.
EXPECT = {
    (LXC, "mapped"): {"idmap": IDMAP},
    (LXC, "conf"): {
        "console": {"type": "console"},
        "pool_id": "gpool",
        "description": "raw lines",
        "lifecycle": {"ignore_changes": ["operating_system", "description"]},
    },
    (VM, "efi"): {
        "scsi_hardware": "virtio-scsi-single",
        "efi_disk": {"datastore_id": "local", "file_format": "raw", "type": "4m", "pre_enrolled_keys": True},
        "initialization": {
            "datastore_id": "local",
            "interface": "ide2",
            "upgrade": True,
            "ip_config": [{"ipv4": {"address": "192.0.2.73/24"}}],
        },
        "clone": {"vm_id": 9000, "full": True},
        "lifecycle": {"ignore_changes": ["clone"]},
        "pool_id": "gpool",
    },
    (POOL, "gpool"): {"pool_id": "gpool", "comment": "guests of the gaps estate"},
}
# Estate gaps: arguments that must not be rendered at all.
ABSENT = {
    (LXC, "mapped"): ["console", "pool_id", "lifecycle"],
    (LXC, "conf"): ["idmap"],
}
PRIMITIVE = {"string": str, "number": (int, float), "bool": bool}
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
        want = PRIMITIVE.get(typ) if isinstance(typ, str) else None
        is_ref = isinstance(value, str) and "${" in value
        if want and not is_ref and (not isinstance(value, want) or (typ == "number" and isinstance(value, bool))):
            problems.append(f"{where}: {value!r} is not a {typ}")
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


def check(doc, schemas, x):
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

    for rtype, name in x["managed"].items():
        if name not in resources.get(rtype, {}):
            problems.append(f"missing managed guest at {rtype}.{name}")
    pool = x["pool"]
    if pool is None:
        if POOL in resources:
            problems.append(f"unexpected {POOL} rendered")
    elif pool not in resources.get(POOL, {}):
        problems.append(f"missing pool at {POOL}.{pool}")
    elif resources[POOL][pool].get("pool_id") != pool:
        problems.append(f"{POOL}.{pool}: pool_id must be '{pool}'")

    adopted = x["adopted"]
    if adopted:
        for rtype, instances in resources.items():
            if adopted in instances:
                problems.append(f"adopted guest rendered at {rtype}.{adopted}")
        unmanaged = doc.get("locals", {}).get("fleet_unmanaged")
        if not isinstance(unmanaged, (list, dict)) or not any(
            adopted in str(u)
            for u in (unmanaged if isinstance(unmanaged, list) else list(unmanaged))
        ):
            problems.append(f"locals.fleet_unmanaged does not list the adopted guest '{adopted}'")

    companions = doc.get("locals", {}).get("fleet_unrendered_companions")
    if x["companion"]:
        if not isinstance(companions, list) or x["companion"] not in companions:
            problems.append(
                f"locals.fleet_unrendered_companions does not list the guest '{x['companion']}'"
            )
        # Its marker lines come back as description text: ignored, not fought.
        ignored = (resources.get(LXC, {}).get(x["companion"], {}).get("lifecycle") or {}).get("ignore_changes") or []
        if "description" not in ignored:
            problems.append(f"{LXC}.{x['companion']}: lifecycle.ignore_changes does not hold description")
    elif companions:
        problems.append(f"unexpected locals.fleet_unrendered_companions {companions}")

    if x is ESTATES["gaps"]:
        for (rtype, name), want in EXPECT.items():
            body = resources.get(rtype, {}).get(name)
            if body is None:
                problems.append(f"missing {rtype}.{name}")
                continue
            for arg, value in want.items():
                if body.get(arg) != value:
                    problems.append(f"{rtype}.{name}.{arg}: {body.get(arg)!r}, expected {value!r}")
            for arg in ABSENT.get((rtype, name), []):
                if arg in body:
                    problems.append(f"{rtype}.{name}.{arg}: rendered, but must be absent")

    endpoint = doc.get("provider", {}).get("proxmox", {}).get("endpoint")
    if endpoint != x["endpoint"]:
        problems.append(f"provider.proxmox.endpoint is '{endpoint}', not the expected '{x['endpoint']}'")

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
                    f"data.sops_file.{name}: source_file is not the owning estate's file '{TOKEN_PATH}'"
                )
            if name != x['file']:
                problems.append(
                    f"{path}: references data.sops_file.{name}, not the expected '{x['file']}'"
                )
            if key != x['key']:
                problems.append(f"{path}: key '{key}' is not the expected key '{x['key']}'")
    if tokens == 0:
        problems.append("no provider api_token rendered")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rendered")
    ap.add_argument("--estate", default="mini", choices=sorted(ESTATES))
    ap.add_argument("--schema", default=SCHEMA)
    a = ap.parse_args()
    with open(a.schema, encoding="utf-8") as f:
        schemas = json.load(f)[PROVIDER]["resource_schemas"]
    with open(a.rendered, encoding="utf-8") as f:
        doc = json.load(f)
    problems = check(doc, schemas, ESTATES[a.estate])
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
