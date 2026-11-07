"""`fleetkit forget` (forget.py) with the fake engine: a resource the program no
longer has is dropped from state, behind its refusals. The Proxmox API is a
stub (`fetch`). Offline."""
import json

import pytest
from click.testing import CliRunner

import fakes
from fakes import CT, POOL, VM
from fleetkit_cli import forget
from fleetkit_cli.events import Emitter
from fleetkit_cli.main import cli

TOKEN = "root@pam!t=00000000-secret-token"
ENDPOINT = "https://pve.example:8006"


def full():
    return fakes.program(
        web={"type": CT, "properties": {"nodeName": "pve1", "vmId": 101}},
        old={"type": VM, "properties": {"nodeName": "pve1", "vmId": 9002}},
        apps={"type": POOL, "properties": {"poolId": "apps"}})


@pytest.fixture
def lab(estate):
    """The program no longer has `old` (VM 9002); state still holds it, protected."""
    p = full()
    w = estate.stack(p)
    w.in_state(p, "provider-proxmox")
    w.state[fakes.urn(p, "provider-proxmox")]["inputs"] = {"endpoint": ENDPOINT, "apiToken": TOKEN}
    w.in_state(p, "web", "101", protect=True)
    w.in_state(p, "old", "9002", protect=True)
    w.in_state(p, "apps", "apps")
    shrunk = json.loads(json.dumps(p))
    del shrunk["resources"]["old"]
    estate.stack(shrunk)
    return estate, w, p


class Cluster:
    def __init__(self, *vmids):
        self.vmids, self.asked = {str(v) for v in vmids}, []

    def __call__(self, endpoint, token, insecure):
        self.asked.append((endpoint, token, insecure))
        return set(self.vmids)


def go(estate, p, key, fetch, apply=False, events=None):
    return forget.run(estate.s, "mini", "mini-guests", fakes.urn(p, key), Emitter((events if events is not None else []).append),
                      apply=apply, fetch=fetch)


def test_report_changes_nothing(lab):
    estate, w, p = lab
    cluster = Cluster(101)
    report = go(estate, p, "old", cluster)
    assert report["forgotten"] is False and report["protected"] and report["id"] == "9002"
    assert report["checked"] == {"endpoint": ENDPOINT, "vmid": "9002", "clusterGuests": 1}
    assert cluster.asked == [(ENDPOINT, TOKEN, False)]
    assert fakes.urn(p, "old") in w.state and w.deleted == [] and w.unprotected == []
    assert "would be removed" in "\n".join(forget.text(report))
    assert estate.runs() == []


def test_apply_unprotects_and_deletes(lab):
    estate, w, p = lab
    events = []
    report = go(estate, p, "old", Cluster(101), apply=True, events=events)
    u = fakes.urn(p, "old")
    assert report["forgotten"] and u not in w.state
    assert w.unprotected == [u] and w.deleted == [u]
    # Nothing else was touched, and no engine run happened.
    assert fakes.urn(p, "web") in w.state and w.state[fakes.urn(p, "web")]["protect"] is True
    assert w.calls == [] and estate.runs() == []
    assert "removed from state" in "\n".join(forget.text(report))
    assert TOKEN not in json.dumps(events)


def test_unprotected_resource_is_not_unprotected_again(lab):
    estate, w, p = lab
    w.state[fakes.urn(p, "old")]["protect"] = False
    assert go(estate, p, "old", Cluster(), apply=True)["forgotten"]
    assert w.unprotected == []


def test_declared_resource_is_refused(lab):
    estate, w, p = lab
    cluster = Cluster()
    with pytest.raises(forget.ForgetError, match="declared by the program of mini-guests"):
        go(estate, p, "web", cluster, apply=True)
    assert cluster.asked == [] and w.deleted == [] and fakes.urn(p, "web") in w.state


def test_not_in_state_is_refused(lab):
    estate, w, p = lab
    del w.state[fakes.urn(p, "old")]
    with pytest.raises(forget.ForgetError, match="is not in the state"):
        go(estate, p, "old", Cluster(), apply=True)


def test_live_guest_is_refused(lab):
    estate, w, p = lab
    with pytest.raises(forget.ForgetError, match="vmid 9002 .* EXISTS in the cluster"):
        go(estate, p, "old", Cluster(101, 9002), apply=True)
    assert fakes.urn(p, "old") in w.state and w.deleted == [] and w.unprotected == []


@pytest.mark.parametrize("inputs", [{"endpoint": ENDPOINT}, {"apiToken": TOKEN}, {}])
def test_unknown_cluster_is_refused(lab, inputs):
    estate, w, p = lab
    w.state[fakes.urn(p, "provider-proxmox")]["inputs"] = inputs
    with pytest.raises(forget.ForgetError, match="no endpoint and apiToken"):
        go(estate, p, "old", Cluster(), apply=True)
    assert w.deleted == []


def test_api_failure_refuses(lab):
    estate, w, p = lab

    def down(endpoint, token, insecure):
        raise forget.ForgetError("the Proxmox API could not be read")

    with pytest.raises(forget.ForgetError, match="could not be read"):
        go(estate, p, "old", down, apply=True)
    assert w.deleted == []


def test_provider_and_non_guest(lab):
    estate, w, p = lab
    with pytest.raises(forget.ForgetError, match="declared by the program"):
        go(estate, p, "provider-proxmox", Cluster(), apply=True)
    # A provider the program no longer has is not forgotten by this command either.
    estate.stack(json.loads(json.dumps({**p, "resources": {"web": p["resources"]["web"]}})))
    with pytest.raises(forget.ForgetError, match="provider or stack resource"):
        go(estate, p, "provider-proxmox", Cluster(), apply=True)
    # A resource that is not a guest needs no cluster check.
    pool = fakes.urn(p, "apps")
    estate.stack(json.loads(json.dumps({**p, "resources": {k: v for k, v in p["resources"].items()
                                                           if k not in ("old", "apps")}})))
    cluster = Cluster()
    report = go(estate, p, "apps", cluster, apply=True)
    assert report["forgotten"] and "checked" not in report and cluster.asked == [] and pool not in w.state


def test_cli(lab, monkeypatch):
    estate, w, p = lab
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    monkeypatch.setattr(forget, "cluster_vmids", Cluster(101))
    base = ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir), "forget", "mini", "mini-guests"]
    u = fakes.urn(p, "old")
    r = CliRunner().invoke(cli, [*base, u, "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["forgotten"] is False and w.deleted == []
    r = CliRunner().invoke(cli, [*base, u, "--apply"])
    assert r.exit_code == 0 and "removed from state" in r.output and w.deleted == [u]
    r = CliRunner().invoke(cli, [*base, u, "--apply"])
    assert r.exit_code == 1 and "is not in the state" in r.output
    r = CliRunner().invoke(cli, [*base, fakes.urn(p, "web"), "--apply"])
    assert r.exit_code == 1 and "declared by the program" in r.output
