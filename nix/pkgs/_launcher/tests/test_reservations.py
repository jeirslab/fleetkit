"""Address / vmid reservations across the shared state backend: the parse of
a tfstate document and the collision rule, on fixtures shaped like the real
thing (two fleets, one cluster, one pg backend)."""
from __future__ import annotations

from fleet_launcher.reservations import (
    Reservation, declared_hosts, find_conflicts, parse_state,
)


def _ct(name, vmid, ip, node="rippy"):
    return {
        "mode": "managed", "type": "proxmox_virtual_environment_container", "name": name,
        "instances": [{"attributes": {
            "vm_id": vmid, "node_name": node,
            "initialization": [{"ip_config": [{"ipv4": [{"address": f"{ip}/24", "gateway": "10.1.1.1"}]}]}],
        }}],
    }


HOMELAB_TOOLS = {"resources": [
    _ct("mcp-proxy", 115, "10.1.1.53"),
    _ct("mealie", 117, "10.1.1.55"),
    {"mode": "data", "type": "proxmox_virtual_environment_container", "name": "adopted",
     "instances": [{"attributes": {"vm_id": 999}}]},
]}
XGCS_DEV = {"resources": [
    _ct("dev-xander", 161, "10.1.1.53"),
    _ct("dev-bala", 162, "10.1.1.54"),
    {"mode": "managed", "type": "proxmox_virtual_environment_vm", "name": "gpu-1",
     "instances": [{"attributes": {"vm_id": 160, "node_name": "rippy",
                                   "initialization": [{"ip_config": [{"ipv4": [{"address": "dhcp"}]}]}]}}]},
]}
XGCS_CORE = {"resources": [_ct("postgres", 102, "10.1.1.5")]}
HOMELAB_DATA = {"resources": [_ct("postgres", 102, "10.1.1.5")]}


def test_parse_state_reads_ips_vmids_and_skips_data_sources_and_dhcp():
    r = parse_state("tf_platform-tools", HOMELAB_TOOLS)
    assert [x.name for x in r] == ["mcp-proxy", "mealie"]
    assert r[0] == Reservation("tf_platform-tools", "proxmox_virtual_environment_container.mcp-proxy",
                               "mcp-proxy", ("10.1.1.53",), 115, "rippy")
    gpu = [x for x in parse_state("tf_xgcs-dev", XGCS_DEV) if x.name == "gpu-1"][0]
    assert gpu.ips == () and gpu.vmid == 160


def test_declared_hosts_fall_back_to_internal_ip():
    d = declared_hosts({"mcp-proxy": {"ip": "", "internal_ip": "10.1.1.53", "vmid": 115},
                        "caddy": {"ip": "10.1.1.129", "vmid": "121"}})
    assert d == {"mcp-proxy": ("10.1.1.53", 115), "caddy": ("10.1.1.129", 121)}


def _all():
    return (parse_state("tf_platform-tools", HOMELAB_TOOLS) + parse_state("tf_xgcs-dev", XGCS_DEV)
            + parse_state("tf_xgcs-core", XGCS_CORE) + parse_state("tf_platform-data", HOMELAB_DATA))


def test_a_host_whose_address_another_state_holds_is_a_conflict():
    c = find_conflicts({"mcp-proxy": ("10.1.1.53", 115)}, _all())
    assert len(c) == 1
    assert c[0].what == "ip 10.1.1.53" and c[0].held_by.name == "dev-xander" and c[0].kind == "reserved"


def test_a_vmid_another_state_holds_is_a_conflict_even_with_a_free_address():
    c = find_conflicts({"infra-db": ("10.1.1.99", 162)}, _all())
    assert [(x.what, x.held_by.name) for x in c] == [("vmid 162", "dev-bala")]


def test_the_same_name_in_two_states_is_a_double_declaration():
    c = find_conflicts({"postgres": ("10.1.1.5", 102)}, _all())
    assert {x.kind for x in c} == {"declared twice"}
    assert {x.held_by.schema for x in c} == {"tf_xgcs-core", "tf_platform-data"}


def test_own_resource_and_free_addresses_are_not_conflicts():
    assert find_conflicts({"mealie": ("10.1.1.55", 117), "new-box": ("10.1.1.69", 140)}, _all()) == []
