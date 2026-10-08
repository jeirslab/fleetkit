"""The type lists cannot fall behind the provider (guests.py): every resource
token of the pinned bpg/proxmox name map is in exactly one of them, and one
that matches vm / container / lxc is either a guest or listed as not one, with
a reason. Offline.

The name map is not part of the package source (cli/); the package build
passes its directory in FLEETKIT_PROVIDER_NAMES, a checkout has it at
../providers/pulumi/names. tests/guest_types.py is the same check as a gate."""
import json
import os
import re
import subprocess
import sys
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


def every(names_file):
    return sorted(r["token"] for r in json.loads(Path(names_file).read_text())["resources"].values())


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
    # Every resource type of the provider is in exactly one list: what is in
    # none, the guard refuses at run time (guard.unknown).
    known = sorted({t for f in files for t in every(f)})
    lists = [set(guests.GUEST_TYPES), set(guests.HA_TYPES), set(guests.NOT_GUESTS), set(guests.OTHER_TYPES)]
    assert [t for t in known if sum(t in x for x in lists) != 1] == []
    assert [t for t in known if guard.unknown(t)] == [] and len(known) > 100
    # Nothing stale either: every listed token exists in the pinned provider.
    assert sorted(set().union(*lists) - set(known)) == [] and set().union(*lists) == guests.KNOWN_TYPES
    assert not [t for t in tokens if t in guests.OTHER_TYPES or t in guests.HA_TYPES]
    assert all(len(why) > 20 for why in guests.NOT_GUESTS.values())
    assert guard.GUEST_TYPES is guests.GUEST_TYPES
    assert sorted(t.rsplit(":", 1)[-1] for t in guests.HA_TYPES) == ["Haresource", "VirtualEnvironmentHaresource"]


def test_a_type_the_lists_do_not_have_fails_the_gate(tmp_path):
    """tests/guest_types.py, the gate itself, on a name map with one more
    type: whatever its name, it must be decided (guests.py) before the gate
    passes, and until then the guard refuses it."""
    d = names_dir()
    gate = Path(__file__).resolve().parents[2] / "tests" / "guest_types.py"
    if d is None or not gate.is_file():
        pytest.skip("the gate (tests/guest_types.py) and the provider name map are not reachable from here "
                    "(the package build has cli/ only)")
    pinned = sorted(d.glob("bpg-proxmox-*.json"))[-1]
    assert subprocess.run([sys.executable, str(gate), str(d)], capture_output=True).returncode == 0
    for name, token in (("proxmox_guest_agent_exec", "proxmox:index/guestAgentExec:GuestAgentExec"),
                        ("proxmox_qemu", "proxmox:index/qemu:Qemu")):
        doc = json.loads(pinned.read_text())
        doc["resources"][name] = {"token": token}
        (tmp_path / pinned.name).write_text(json.dumps(doc))
        r = subprocess.run([sys.executable, str(gate), str(tmp_path)], capture_output=True, text=True)
        assert r.returncode == 1 and f"unknown: {token}" in r.stderr and '"guest_types": "fail"' in r.stdout
        assert guard.unknown(token)
    # One that is gone from the provider is stale.
    doc = json.loads(pinned.read_text())
    del doc["resources"]["proxmox_haresource"]
    (tmp_path / pinned.name).write_text(json.dumps(doc))
    r = subprocess.run([sys.executable, str(gate), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 1 and "stale: proxmox:index/haresource:Haresource" in r.stderr


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
