#!/usr/bin/env python3
"""Guest model <-> bpg/proxmox provider fidelity gate.

Checks docs/guest-provider-map.md against the pinned provider schema JSON and
against fleet.report.guestOptionPaths. Evaluation only; no network.

  --gate             run all checks + selftest, exit 1 on any problem
  --selftest         run only the mutation selftest
  --paths-json FILE  read the option paths from FILE (JSON list)
  --skeleton         read them from the architect skeleton eval script
  --map FILE         alternative map file
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = f"{ROOT}/docs/guest-provider-map.md"
SCHEMA = f"{ROOT}/providers/schemas/bpg-proxmox-0.115.0.schema.json"
SKEL = f"{ROOT}/.fleet/wf/arch/skel-eval.sh"
PROVIDER = "registry.opentofu.org/bpg/proxmox"
LXC = "proxmox_virtual_environment_container"
VM = "proxmox_virtual_environment_vm"
LINE = re.compile(r"^(lxc|vm|both|meta|companion|none): (\S+) => (.+)$")
META = {"lifecycle.prevent_destroy", "lifecycle.ignore_changes"}


def parse_map(path):
    out = []  # (lineno, kind, model, target)
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            m = LINE.match(line.rstrip("\n"))
            if m:
                out.append((n, m.group(1), m.group(2), m.group(3).strip()))
    return out


def load_schema():
    with open(SCHEMA, encoding="utf-8") as f:
        d = json.load(f)
    return d[PROVIDER]["resource_schemas"]


def attr_obj_members(typ):
    """If typ is list/set/tuple/map of object, return the object's keys."""
    if isinstance(typ, list) and len(typ) == 2 and typ[0] in ("list", "set", "map"):
        inner = typ[1]
        if isinstance(inner, list) and len(inner) == 2 and inner[0] == "object":
            return set(inner[1].keys())
    if isinstance(typ, list) and len(typ) == 2 and typ[0] == "object":
        return set(typ[1].keys())
    return None


def path_exists(schemas, rtype, path):
    res = schemas.get(rtype)
    if res is None:
        return False
    block = res["block"]
    segs = path.split(".")
    for i, seg in enumerate(segs):
        if block is None:
            return False
        rest = segs[i + 1:]
        attrs = block.get("attributes", {})
        bts = block.get("block_types", {})
        if seg in bts:
            block = bts[seg]["block"]
            if not rest:
                return True
            continue
        if seg in attrs:
            if not rest:
                return True
            members = attr_obj_members(attrs[seg].get("type"))
            if members is None:
                return False
            # remaining segments are object keys (nested objects not needed)
            return len(rest) == 1 and rest[0] in members
        return False
    return False


def check_target(schemas, kind, target):
    """Return None if fine, else a problem description."""
    if kind == "lxc":
        return None if path_exists(schemas, LXC, target) else f"'{target}' not in {LXC}"
    if kind == "vm":
        return None if path_exists(schemas, VM, target) else f"'{target}' not in {VM}"
    if kind == "both":
        miss = [r for r in (LXC, VM) if not path_exists(schemas, r, target)]
        return None if not miss else f"'{target}' not in {', '.join(miss)}"
    if kind == "meta":
        return None if target in META else f"meta target '{target}' not one of {sorted(META)}"
    if kind == "companion":
        return None if target.startswith("terraform_data.") else f"companion target '{target}' must start with terraform_data."
    if kind == "none":
        return None if target.strip() else "empty reason"
    return f"unknown kind {kind}"


def run_checks(map_path, paths, schemas):
    """Return (problems, counts)."""
    mappings = parse_map(map_path)
    problems = []
    bad = 0
    for n, kind, model, target in mappings:
        err = check_target(schemas, kind, target)
        if err:
            bad += 1
            problems.append(f"FAIL {kind}: {model} (map line {n}): {err}")
    pathset = set(paths)
    mapped = {m for _, _, m, _ in mappings}
    unmapped = sorted(pathset - mapped)
    stale = sorted(mapped - pathset)
    for p in unmapped:
        problems.append(f"FAIL unmapped: option path '{p}' has no mapping line")
    for p in stale:
        problems.append(f"FAIL stale: map names model path '{p}' which is not a guest option path")
    return problems, {
        "mappings": len(mappings),
        "paths": len(pathset),
        "badTargets": bad,
        "unmapped": len(unmapped),
        "stale": len(stale),
    }


def get_paths_json(file):
    with open(file, encoding="utf-8") as f:
        return json.load(f)


def get_paths_skeleton():
    env = dict(os.environ, UNCHECKED="1")
    r = subprocess.run(
        ["bash", SKEL, "", "f: f.report.guestOptionPaths"],
        capture_output=True, text=True, env=env,
    )
    if r.returncode != 0:
        raise RuntimeError("skeleton eval failed: " + r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "skeleton eval failed")
    return json.loads(r.stdout)


def get_paths_flake():
    subprocess.run(
        ["flock", f"{ROOT}/.fleet/wf/git.lock", "git", "-C", ROOT, "add", "-A"],
        capture_output=True, check=False,
    )
    r = subprocess.run(
        ["nix", "eval", "--json", f"{ROOT}#fleet.report.guestOptionPaths"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines()
        raise RuntimeError("flake eval failed: " + (lines[-1] if lines else "?"))
    return json.loads(r.stdout)


def selftest(schemas, paths):
    """Mutate a COPY of the map; each mutation must be caught. Returns True if all pass."""
    with open(MAP, encoding="utf-8") as f:
        orig = f.read().split("\n")
    pathset = set(paths)
    ok = True

    def first_idx(pred):
        for i, l in enumerate(orig):
            if pred(l):
                return i
        raise SystemExit(f"selftest setup: no line matching in map")

    def mutate(name, fn, expect):
        nonlocal ok
        lines = list(orig)
        fn(lines)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "map.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            _, c = run_checks(p, paths, schemas)
        caught = c[expect] > 0
        # others must stay at zero for a clean attribution
        clean = all(c[k] == 0 for k in ("badTargets", "unmapped", "stale") if k != expect)
        good = caught and clean
        ok &= good
        print(f"{'PASS' if good else 'FAIL'} selftest {name}: {expect}={c[expect]}")

    def replace(old, new):
        def fn(lines):
            i = first_idx(lambda l: l == old)
            lines[i] = new
        return fn

    def delete_model(model):
        def fn(lines):
            hit = [i for i, l in enumerate(lines)
                   if (m := LINE.match(l)) and m.group(2) == model]
            if len(hit) != 1:
                raise SystemExit(f"selftest setup: '{model}' has {len(hit)} lines, need exactly 1")
            del lines[hit[0]]
        return fn

    def append(line):
        return lambda lines: lines.append(line)

    # Baseline must be clean for attribution to mean anything.
    _, base = run_checks(MAP, paths, schemas)
    if any(base[k] for k in ("badTargets", "unmapped", "stale")):
        print(f"FAIL selftest baseline not clean: {base}")
        ok = False

    mutate("lxc target typo",
           replace("lxc: networkInterfaces.*.bridge => network_interface.bridge",
                   "lxc: networkInterfaces.*.bridge => network_interface.bridgex"),
           "badTargets")
    mutate("both with container-only target",
           replace("lxc: startOnBoot => start_on_boot",
                   "both: startOnBoot => start_on_boot"),
           "badTargets")
    mutate("delete vmid line", delete_model("vmid"), "unmapped")
    mutate("stale model path",
           append("both: noSuchOption.nowhere => description"), "stale")
    mutate("meta bad lifecycle",
           append("meta: protect => lifecycle.create_before_destroy"), "badTargets")
    mutate("vm attribute-object member typo",
           replace("vm: networkInterfaces.*.model => network_device.model",
                   "vm: networkInterfaces.*.model => network_device.modelx"),
           "badTargets")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--paths-json")
    ap.add_argument("--skeleton", action="store_true")
    ap.add_argument("--map", default=MAP)
    a = ap.parse_args()
    if not (a.gate or a.selftest):
        ap.error("need --gate or --selftest")

    schemas = load_schema()
    paths = None
    if a.paths_json:
        paths = get_paths_json(a.paths_json)
    elif a.skeleton:
        paths = get_paths_skeleton()
    else:
        # No silent fallback to the architect skeleton: the gate checks the
        # live model or fails.
        try:
            paths = get_paths_flake()
        except RuntimeError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            print(json.dumps({"ok": False, "error": "flake eval of guestOptionPaths failed"}))
            sys.exit(1)

    if a.selftest and not a.gate:
        sys.exit(0 if selftest(schemas, paths) else 1)

    problems, counts = run_checks(a.map, paths, schemas)
    for p in problems:
        print(p, file=sys.stderr)
    st = selftest(schemas, paths)
    ok = not problems and st
    print(json.dumps({"ok": ok, **counts, "selftest": "pass" if st else "fail"}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
