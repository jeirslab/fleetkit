"""`fleetkit adopt` (adopt.py) with the fake engine: what is to adopt, the
report, the two refusals of --apply, the check after it, and that `import` is
only ever in a temporary program that is gone afterwards. Offline."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import fakes
from fakes import CT, POOL, VM
from fleetkit_cli import adopt
from fleetkit_cli.events import Emitter
from fleetkit_cli.main import cli

WEB = {"nodeName": "pve1", "vmId": 101, "cores": 2}
DB = {"nodeName": "pve1", "vmId": 102}


def prog():
    return fakes.program(
        web={"type": CT, "properties": WEB, "options": {"protect": True}},
        db={"type": VM, "properties": DB},
        apps={"type": POOL, "properties": {"poolId": "apps"}},
        old={"type": CT, "properties": {"nodeName": "pve2", "vmId": 300}})


@pytest.fixture
def lab(estate):
    """web and db exist for real and are not in state; apps is in state; old
    has no adoption id (the model cannot tell)."""
    p = prog()
    w = estate.stack(p, adopt_ids={"web": "pve1/101", "db": "pve1/102", "apps": "apps"},
                     unresolved={"old": "the guest has no vmid in the model"})
    w.live[(CT, "pve1/101")] = dict(WEB)
    w.live[(VM, "pve1/102")] = dict(DB)
    w.in_state(p, "provider-proxmox")
    w.in_state(p, "apps", "apps")
    return estate, w, p


def run(estate, events=None, **kw):
    return adopt.run(estate.s, "mini", Emitter((events if events is not None else []).append), **kw)


def statuses(report, stack="mini-guests"):
    return {r["key"]: r["status"] for r in report["stacks"][stack]["resources"]}


def no_import_left(estate, w):
    """import was only ever in programs outside the real work dir, and those are gone."""
    real = str(estate.workdir())
    with_import = [c for c in w.calls if c["imports"]]
    assert all(c["wd"] != real and not c["wd"].startswith(str(estate.s.state_dir)) for c in with_import)
    assert all(not Path(c["wd"]).exists() for c in with_import)
    assert estate.import_anywhere() == []
    assert "import" not in json.loads((estate.workdir() / "Pulumi.yaml").read_text())["resources"]["web"]["options"]
    return with_import


def test_todo_in_state_not_in_state_unresolved(lab):
    estate, w, p = lab
    report = run(estate)
    assert statuses(report) == {"web": "import", "db": "import", "apps": "in-state", "old": "unresolved"}
    res = {r["key"]: r for r in report["stacks"]["mini-guests"]["resources"]}
    assert "does not touch" in res["apps"]["note"] and res["old"]["why"] == "the guest has no vmid in the model"
    assert res["web"]["id"] == "pve1/101" and res["web"]["urn"].endswith(f"::{CT}::web")
    assert report["ok"] and not report["applied"] and report["refused"] == []
    # Only the todo resources carried import, and the run was targeted at them
    # and at the provider they need.
    (call,) = no_import_left(estate, w)
    assert call["verb"] == "preview" and call["imports"] == {"web": "pve1/101", "db": "pve1/102"}
    assert sorted(u.rsplit("::", 1)[1] for u in call["targets"]) == ["db", "provider-proxmox", "web"]
    text = "\n".join(adopt.text(report))
    assert "web: import pve1/101" in text and "old: cannot adopt: the guest has no vmid" in text
    assert "apps: already in state" in text and "nothing changed (pass --apply to adopt)" in text


def test_id_option_resolves_overrides_and_selects(lab):
    estate, w, p = lab
    w.live[(CT, "pve2/300")] = {"nodeName": "pve2", "vmId": 300}
    report = run(estate, ids={"old": "pve2/300"}, resources=["old", "apps"])
    assert statuses(report) == {"old": "import", "apps": "in-state"}
    assert no_import_left(estate, w)[0]["imports"] == {"old": "pve2/300"}
    # --id wins over the stack's adoptIds; a wrong id is the provider's error.
    report = run(estate, ids={"web": "pve1/999"}, resources=["web"])
    (r,) = report["stacks"]["mini-guests"]["resources"]
    assert r["status"] == "error" and "'pve1/999' does not exist" in r["error"] and not report["ok"]
    # An id for a resource in state changes nothing: adopt does not touch it.
    assert statuses(run(estate, ids={"apps": "other"}, resources=["apps"])) == {"apps": "in-state"}
    # A resource with no id at all, asked for by name.
    estate.stacks["mini-guests"]["adoptUnresolved"] = {}
    assert statuses(run(estate, resources=["old"])) == {"old": "no-id"}
    with pytest.raises(adopt.AdoptError, match="--id: no resource nope"):
        run(estate, ids={"nope": "1"})
    with pytest.raises(adopt.AdoptError, match="--resource: no resource nope"):
        run(estate, resources=["nope"])


def test_without_apply_nothing_changes(lab):
    estate, w, p = lab
    w.live[(CT, "pve1/101")]["cores"] = 4
    before = (json.dumps(w.state, sort_keys=True), repr(sorted(w.live.items())))
    report = run(estate)
    assert [c["verb"] for c in w.calls] == ["preview"]
    assert (json.dumps(w.state, sort_keys=True), repr(sorted(w.live.items()))) == before
    assert w.updated == [] and w.created == [] and w.destroyed == []
    web = next(r for r in report["stacks"]["mini-guests"]["resources"] if r["key"] == "web")
    assert web["status"] == "import+update"
    assert web["diff"] == [{"path": "cores", "live": 4, "declared": 2}]
    # A difference is the report's content: ok, with what an apply would refuse.
    assert report["ok"] and [r["key"] for r in report["refused"]] == ["web"]
    assert "cores: live 4, declared 2" in "\n".join(adopt.text(report))
    no_import_left(estate, w)


def test_apply_adopts_and_verifies(lab):
    estate, w, p = lab
    events = []
    report = run(estate, events, apply=True)
    assert report["ok"] and report["applied"] and report["stacks"]["mini-guests"]["verify"] == {"ok": True, "differs": []}
    assert fakes.urn(p, "web") in w.state and fakes.urn(p, "db") in w.state
    assert w.state[fakes.urn(p, "web")]["id"] == "pve1/101"
    assert w.updated == [] and w.created == [] and w.destroyed == []
    real = str(estate.workdir())
    assert [(c["verb"], bool(c["imports"]), c["wd"] == real) for c in w.calls] == [
        ("preview", True, False), ("up", True, False), ("preview", False, True)]
    assert w.calls[1]["targets"] == w.calls[0]["targets"] and w.calls[2]["targets"] is None
    no_import_left(estate, w)
    # A second run finds nothing to do and starts no engine run.
    n = len(w.calls)
    assert statuses(run(estate, apply=True))["web"] == "in-state" and len(w.calls) == n


def test_apply_refuses_an_update_that_is_not_accepted(lab):
    estate, w, p = lab
    w.live[(CT, "pve1/101")]["cores"] = 4
    report = run(estate, apply=True)
    assert not report["ok"] and not report["applied"]
    (r,) = report["refused"]
    assert r["key"] == "web" and "cores" in r["why"] and "reboots" in r["why"] and "--accept-update web" in r["why"]
    assert [c["verb"] for c in w.calls] == ["preview"] and fakes.urn(p, "web") not in w.state
    assert fakes.urn(p, "db") not in w.state  # nothing of the run was applied
    no_import_left(estate, w)
    # Accepting another resource does not accept this one.
    assert not run(estate, apply=True, accept_update=["db"])["applied"]
    # Accepted: imported and updated in place.
    report = run(estate, apply=True, accept_update=["web"])
    assert report["ok"] and report["applied"] and w.updated == ["web"]
    assert w.live[(CT, "pve1/101")]["cores"] == 2
    no_import_left(estate, w)


@pytest.mark.parametrize("steps", [["create"], ["create-replacement", "replace", "delete-replaced"], ["delete"],
                                   ["import-replacement"]])
def test_apply_refuses_anything_but_import_update_same(lab, steps):
    estate, w, p = lab
    w.force["db"] = {"steps": steps}
    report = run(estate, apply=True, accept_update=["web", "db"])
    assert not report["ok"] and not report["applied"]
    assert [c["verb"] for c in w.calls] == ["preview"] and w.destroyed == []
    assert any(r["key"] == "db" and "adopt only imports" in r["why"] for r in report["refused"])
    assert fakes.urn(p, "web") not in w.state
    no_import_left(estate, w)


def test_apply_refuses_a_create_of_a_dependency(lab):
    estate, w, p = lab
    p["resources"]["web"]["properties"]["poolId"] = "${net.poolId}"
    p["resources"]["net"] = {"type": POOL, "properties": {"poolId": "net"},
                             "options": {"provider": "${provider-proxmox}"}}
    estate.stack(p, adopt_ids={"web": "pve1/101"})
    w.live[(CT, "pve1/101")]["poolId"] = "${net.poolId}"
    report = run(estate, apply=True)
    assert not report["applied"] and w.created == []
    assert report["stacks"]["mini-guests"]["other"][0]["key"] == "net"
    assert any("create net" in r["why"] for r in report["refused"])


def test_a_new_stack_may_create_its_provider(lab):
    estate, w, p = lab
    w.state.clear()
    report = run(estate, apply=True, resources=["web"])
    assert report["ok"] and report["applied"] and w.created == ["provider-proxmox"]
    assert [c["key"] for c in report["stacks"]["mini-guests"]["other"]] == ["provider-proxmox"]


def test_temporary_program_is_gone_when_the_engine_raises(lab):
    estate, w, p = lab
    w.fail["up"] = RuntimeError("the engine died")
    with pytest.raises(RuntimeError, match="the engine died"):
        run(estate, apply=True)
    assert [c["verb"] for c in no_import_left(estate, w)] == ["preview", "up"]
    # And when the preview raises.
    w.calls.clear()
    w.state.pop(fakes.urn(p, "web"), None)
    w.state.pop(fakes.urn(p, "db"), None)
    w.fail = {"preview": RuntimeError("no route to host")}
    report = run(estate, apply=True)
    assert not report["ok"] and not report["applied"] and "no route to host" in report["stacks"]["mini-guests"]["error"]
    assert [c["verb"] for c in no_import_left(estate, w)] == ["preview"]


def test_post_adoption_preview_that_still_differs_fails(lab, monkeypatch):
    estate, w, p = lab
    w.live[(CT, "pve1/101")]["cores"] = 4
    w.import_keeps_live = True  # the import took the live inputs; the declaration still differs
    report = run(estate, apply=True, accept_update=["web"])
    assert report["applied"] and not report["ok"]
    v = report["stacks"]["mini-guests"]["verify"]
    assert not v["ok"] and [(c["key"], c["op"], c["diff"]) for c in v["differs"]] == [("web", "update", ["cores"])]
    assert w.calls[-1]["verb"] == "preview" and not w.calls[-1]["imports"] and w.updated == []
    assert "NOT VERIFIED" in "\n".join(adopt.text(report))
    no_import_left(estate, w)
    # The command exits non-zero and prints what differs.
    w.state.pop(fakes.urn(p, "web"))
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    r = CliRunner().invoke(cli, ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir),
                                 "adopt", "mini", "--apply", "--accept-update", "web"])
    assert r.exit_code == 1 and "NOT VERIFIED" in r.output and "update web [cores]" in r.output


def test_real_program_with_import_is_refused(lab):
    estate, w, p = lab
    p["resources"]["web"]["options"]["import"] = "pve1/101"
    estate.stack(p, adopt_ids={"web": "pve1/101"})
    with pytest.raises(adopt.AdoptError, match="already sets options.import on web"):
        run(estate, apply=True)
    assert w.calls == []


def test_secrets_in_the_diff_are_redacted(lab):
    estate, w, p = lab
    token = "s3cr3t-token-value"
    w.live[(CT, "pve1/101")]["cores"] = {"4dabf18193072939515e22adb298388d": "1b47", "value": 9}
    w.live[(CT, "pve1/101")]["nodeName"] = f"node-{token}"
    ev = Emitter(lambda e: None)
    ev.secret(token)
    report = adopt.run(estate.s, "mini", ev, resources=["web"])
    diff = {d["path"]: d for d in report["stacks"]["mini-guests"]["resources"][0]["diff"]}
    assert diff["cores"]["live"] == "[secret]" and diff["nodeName"]["live"] == "node-[secret]"
    assert token not in json.dumps(report)


def test_cli_json_is_one_document_and_the_exit_code(lab, monkeypatch):
    estate, w, p = lab
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    base = ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir), "adopt", "mini"]
    r = CliRunner().invoke(cli, [*base, "--json", "--stack", "mini-guests"])
    doc = json.loads(r.stdout)
    assert r.exit_code == 0 and doc["applied"] is False and statuses(doc)["web"] == "import"
    assert [c["verb"] for c in w.calls] == ["preview"]
    w.live[(VM, "pve1/102")]["nodeName"] = "pve9"
    r = CliRunner().invoke(cli, [*base, "--apply"])
    assert r.exit_code == 1 and "refused: mini-guests db" in r.output and "--accept-update db" in r.output
    r = CliRunner().invoke(cli, [*base, "--apply", "--accept-update", "db", "--id", "web=pve1/101", "--json"])
    doc = json.loads(r.stdout)
    assert r.exit_code == 0 and doc["applied"] and doc["ok"] and w.updated == ["db"]
    assert CliRunner().invoke(cli, [*base, "--id", "web"]).exit_code == 1
    assert CliRunner().invoke(cli, [*base, "--stack", "nope"]).exit_code == 1
