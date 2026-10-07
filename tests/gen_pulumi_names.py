#!/usr/bin/env python3
"""Regenerate providers/pulumi/names/<provider>-<ver>.json: how each Terraform
name of a pinned provider is spelled in its Pulumi bridge.

  gen_pulumi_names.py

Pairs providers/schemas/<p>-<v>.schema.json (tofu providers schema -json) with
providers/pulumi/schemas/<p>-<v>.pulumi.json (pulumi package get-schema
terraform-provider@<bridge> <source> <version>). lib/pulumi.nix reads only the
generated name maps; it never sees either schema.

The bridge does not just camelCase. A list or set with more than one item is
pluralised (network_interface -> networkInterfaces), a block or list limited
to one item is flattened to an object (initialization, disk on a container),
and an input named `id` becomes <resource>Id. So every name is matched against
the Pulumi schema itself, and any Terraform name left without a Pulumi
counterpart fails the run.

Per field: {"n": pulumi name, "arr": true when Pulumi takes a list (otherwise
a one-item Terraform list is an object there), "map": true when the keys are
data (not names), "f": nested fields}.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRIDGE = "1.4.0"  # pulumi-terraform-provider; the version every map is made with
OUT = ROOT / "providers/pulumi/names"


def camel(s):
    head, *rest = s.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def singulars(p):
    out = {p}
    if p.endswith("ies"):
        out.add(p[:-3] + "y")
    if p.endswith("es"):
        out.add(p[:-2])
    if p.endswith("s"):
        out.add(p[:-1])
    return out


class Gen:
    def __init__(self, pschema, label):
        self.pkg = pschema["name"]
        self.types = pschema.get("types", {})
        self.label = label
        self.missing = []

    def ptype(self, prop):
        """Pulumi property -> (is_array, is_map, nested properties or None)."""
        arr = prop.get("type") == "array"
        mp = prop.get("type") == "object" and "additionalProperties" in prop
        inner = prop.get("items") if arr else prop.get("additionalProperties") if mp else prop
        ref = (inner or {}).get("$ref", "")
        props = None
        if ref.startswith("#/types/"):
            props = self.types.get(ref[len("#/types/"):], {}).get("properties", {})
        return arr, mp, props

    def tf_children(self, spec):
        """TF attribute or block spec -> {name: child spec} of its object members."""
        if "block" in spec:  # a block type
            b = spec["block"]
            return {**{n: a for n, a in b.get("attributes", {}).items()},
                    **{n: bt for n, bt in b.get("block_types", {}).items()}}
        if "nested_type" in spec:
            return dict(spec["nested_type"].get("attributes", {}))
        t = spec.get("type")
        while isinstance(t, list) and t[0] in ("list", "set", "map"):
            t = t[1]
        if isinstance(t, list) and t[0] == "object":
            return {n: {"type": ty} for n, ty in t[1].items()}
        return None

    def tf_map(self, spec):
        if "nested_type" in spec:
            return spec["nested_type"].get("nesting_mode") == "map"
        t = spec.get("type")
        return isinstance(t, list) and t[0] == "map"

    def fields(self, tf, pulumi, where, owner=None):
        """tf: {name: spec}; pulumi: {name: prop} -> name map."""
        left = dict(pulumi)
        out = {}
        # Exact names first, so a fallback (id -> <owner>Id, a plural) never
        # takes a name that a Terraform field owns exactly: github_issue has
        # both id and issue_id, and the bridge spells its id githubIssueId.
        exact = {t: camel(t) for t in tf if t != "id" and camel(t) in left}
        for t in sorted(tf, key=lambda t: (t not in exact, t)):
            spec = tf[t]
            c = camel(t)
            if t in exact:
                p = c
            else:
                ids = [f"{owner}Id", f"{self.pkg}{owner[:1].upper()}{owner[1:]}Id"] if t == "id" and owner else []
                cand = [i for i in ids if i in left][:1] or [
                    p for p in left if c in singulars(p) or camel(t + "s") == p]
                if not cand and t == "id" and owner:
                    # A computed-only id is the resource's own id in Pulumi.
                    out[t] = {"n": "id"}
                    continue
                if len(cand) != 1:
                    self.missing.append(f"{where}.{t}")
                    continue
                p = cand[0]
            prop = left.pop(p)
            e = {"n": p}
            arr, mp, props = self.ptype(prop)
            if arr:
                e["arr"] = True
            if self.tf_map(spec):
                e["map"] = True
            kids = self.tf_children(spec)
            if kids and props is not None:
                e["f"] = self.fields(kids, props, f"{where}.{t}")
            out[t] = e
        return out


def entity(g, block, pulumi_props, where, owner=None):
    tf = {**block.get("attributes", {}), **block.get("block_types", {})}
    return g.fields(tf, pulumi_props, where, owner)


def token_for(tokens, tf_type, prefix):
    short = tf_type[len(prefix) + 1:] if tf_type.startswith(prefix + "_") else tf_type
    want = camel(short).lower()
    hits = [k for k in tokens if k.split(":")[-1].lower() in (want, "get" + want)]
    return hits[0] if len(hits) == 1 else None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    bad = 0
    for psch in sorted((ROOT / "providers/pulumi/schemas").glob("*.pulumi.json")):
        name = psch.name[: -len(".pulumi.json")]
        tsch = ROOT / "providers/schemas" / f"{name}.schema.json"
        tdoc = json.loads(tsch.read_text())
        pdoc = json.loads(psch.read_text())
        (addr, tp), = tdoc.items()
        source = "/".join(addr.split("/")[-2:])
        version = name.rsplit("-", 1)[1]
        pkg = pdoc["name"]
        g = Gen(pdoc, name)

        prov_props = {**pdoc.get("config", {}).get("variables", {}),
                      **pdoc.get("provider", {}).get("inputProperties", {})}
        out = {
            "package": pkg,
            "source": source,
            "version": version,
            "bridge": BRIDGE,
            "provider": entity(g, tp["provider"]["block"], prov_props, f"{pkg}.provider"),
            "resources": {},
            "dataSources": {},
        }
        for rt, rs in sorted(tp.get("resource_schemas", {}).items()):
            tok = token_for(pdoc["resources"], rt, pkg)
            if tok is None:
                g.missing.append(f"resource {rt}: no token")
                continue
            r = pdoc["resources"][tok]
            props = {**r.get("properties", {}), **r.get("inputProperties", {})}
            owner = tok.split("/")[-1].split(":")[0]
            out["resources"][rt] = {"token": tok, "f": entity(g, rs["block"], props, rt, owner)}
        for dt, ds in sorted(tp.get("data_source_schemas", {}).items()):
            tok = token_for(pdoc["functions"], dt, pkg)
            if tok is None:
                g.missing.append(f"data source {dt}: no token")
                continue
            fn = pdoc["functions"][tok]
            props = {**fn.get("outputs", {}).get("properties", {}),
                     **fn.get("inputs", {}).get("properties", {})}
            out["dataSources"][dt] = {"token": tok, "f": entity(g, ds["block"], props, dt)}

        (OUT / f"{name}.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
        print(f"{name}: {len(out['resources'])} resources, {len(out['dataSources'])} data sources, "
              f"{len(g.missing)} unmatched", file=sys.stderr)
        for m in g.missing:
            print(f"  unmatched: {m}", file=sys.stderr)
        bad += len(g.missing)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
