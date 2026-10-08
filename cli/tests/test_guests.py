"""The guest list cannot fall behind the provider (guests.py): every resource
token of the pinned bpg/proxmox name map that matches vm / container / lxc is
either a guest or listed as not one, with a reason. Offline.

The name map is not part of the package source (cli/); the package build
passes its directory in FLEETKIT_PROVIDER_NAMES, a checkout has it at
../providers/pulumi/names. tests/guest_types.py is the same check as a gate."""
import json
import os
import re
from pathlib import Path

import pytest

from fleetkit_cli import guard, guests


def names_dir():
    for d in (os.environ.get("FLEETKIT_PROVIDER_NAMES"),
              Path(__file__).resolve().parents[2] / "providers" / "pulumi" / "names"):
        if d and list(Path(d).glob("bpg-proxmox-*.json")):
            return Path(d)
    return None


def candidates(names_file):
    """Every resource token (not data sources) the pattern matches."""
    resources = json.loads(Path(names_file).read_text())["resources"]
    return sorted(r["token"] for r in resources.values()
                  if re.search(guests.GUEST_PATTERN, r["token"].rsplit(":", 1)[-1], re.I))


def undecided(tokens):
    return [t for t in tokens if t not in guests.GUEST_TYPES and t not in guests.NOT_GUESTS]


def test_every_vm_or_container_token_of_the_provider_is_decided():
    d = names_dir()
    if d is None:
        pytest.skip("the provider name map (providers/pulumi/names) is not reachable from here: "
                    "set FLEETKIT_PROVIDER_NAMES")
    files = sorted(d.glob("bpg-proxmox-*.json"))
    tokens = sorted({t for f in files for t in candidates(f)})
    assert len(tokens) >= 10, tokens
    assert undecided(tokens) == [], (
        "resource types of the provider that look like guests and are in neither guests.GUEST_TYPES nor "
        "guests.NOT_GUESTS (with a reason): the guard would not gate them")
    # Nothing stale either: every listed token exists in the pinned provider.
    assert sorted({*guests.GUEST_TYPES, *guests.NOT_GUESTS} - set(tokens)) == []
    assert not set(guests.GUEST_TYPES) & set(guests.NOT_GUESTS)
    assert all(len(why) > 20 for why in guests.NOT_GUESTS.values())
    assert guard.GUEST_TYPES is guests.GUEST_TYPES


def test_the_provider_local_properties_are_the_timeouts_of_the_pinned_provider():
    """What adopt applies without an acceptance (guests.LOCAL_PROPERTIES): per
    type, exactly the provider's `timeout*` properties, in the bridge's names."""
    d = names_dir()
    if d is None:
        pytest.skip("the provider name map (providers/pulumi/names) is not reachable from here: "
                    "set FLEETKIT_PROVIDER_NAMES")
    by_token = {r["token"]: r for f in sorted(d.glob("bpg-proxmox-*.json"))
                for r in json.loads(f.read_text())["resources"].values()}
    assert len(guests.LOCAL_PROPERTIES) == 2 and set(guests.LOCAL_PROPERTIES) <= guests.GUEST_TYPES
    for token, props in guests.LOCAL_PROPERTIES.items():
        have = {v["n"] for k, v in by_token[token]["f"].items() if k.startswith("timeout")}
        assert have == set(props) and props, token
        assert all(p.startswith("timeout") for p in props)


def test_a_new_guest_type_fails_the_check(tmp_path):
    """A provider bump that adds a VM type must fail, not pass unnoticed."""
    f = tmp_path / "bpg-proxmox-9.9.9.json"
    f.write_text(json.dumps({"resources": {
        "proxmox_virtual_environment_vm": {"token": "proxmox:index/virtualEnvironmentVm:VirtualEnvironmentVm"},
        "proxmox_virtual_environment_pool": {"token": "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool"},
        "proxmox_lxc_template": {"token": "proxmox:index/lxcThing:LxcThing"},
        "proxmox_vm9": {"token": "proxmox:index/vm9:Vm9"}}}))
    assert undecided(candidates(f)) == ["proxmox:index/lxcThing:LxcThing", "proxmox:index/vm9:Vm9"]
