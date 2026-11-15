#!/usr/bin/env python3
"""Cut a large provider's pinned schemas down to the resources the kit renders.

  slim_provider.py TF_SCHEMA.json PULUMI_SCHEMA.json RESOURCE... --out <name>-<version>

TF_SCHEMA is `tofu providers schema -json` for a directory that requires the
provider, PULUMI_SCHEMA is `pulumi package get-schema terraform-provider@<bridge>
<source> <version>`. Writes providers/schemas/<out>.schema.json and
providers/pulumi/schemas/<out>.pulumi.json holding the provider block, its
configuration and the named Terraform resources (with the Pulumi types they
reach); no data sources, no functions. hashicorp/google is 10 MB and 22 MB in
full; one resource is a few hundred KB. A resource not listed here is not in
the schema, so the kit's checks refuse it until it is added and the maps
regenerated (tests/gen_provider_index.py, tests/gen_pulumi_names.py).
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def camel(s):
    head, *rest = s.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def refs(node, acc):
    if isinstance(node, dict):
        r = node.get("$ref", "")
        if r.startswith("#/types/"):
            acc.add(r[len("#/types/"):])
        for v in node.values():
            refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            refs(v, acc)


def main():
    tf_path, p_path, *rest = sys.argv[1:]
    out = rest[rest.index("--out") + 1]
    wanted = [r for r in rest[: rest.index("--out")]]
    tf = json.load(open(tf_path))["provider_schemas"]
    (addr, prov), = tf.items()
    prov = {
        "provider": prov["provider"],
        "resource_schemas": {r: prov["resource_schemas"][r] for r in wanted},
        "data_source_schemas": {},
        "ephemeral_resource_schemas": {},
    }
    ps = json.load(open(p_path))
    pkg = ps["name"]
    keep = {}
    for r in wanted:
        short = camel(r[len(pkg) + 1:]).lower()
        hits = [t for t in ps["resources"] if t.split(":")[-1].lower() == short]
        if len(hits) != 1:
            sys.exit(f"{r}: {len(hits)} Pulumi resources match")
        keep[hits[0]] = ps["resources"][hits[0]]
    need, todo = set(), set()
    refs([ps["provider"], ps["config"], *keep.values()], todo)
    while todo:
        t = todo.pop()
        if t not in need:
            need.add(t)
            refs(ps["types"][t], todo)
    ps["resources"] = keep
    ps["functions"] = {}
    ps["types"] = {t: ps["types"][t] for t in sorted(need)}
    (ROOT / "providers/schemas" / f"{out}.schema.json").write_text(json.dumps({addr: prov}, separators=(",", ":")) + "\n")
    (ROOT / "providers/pulumi/schemas" / f"{out}.pulumi.json").write_text(json.dumps(ps, indent=2) + "\n")
    print(f"{out}: {len(keep)} resources, {len(need)} types", file=sys.stderr)


if __name__ == "__main__":
    main()
