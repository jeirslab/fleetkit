"""`fleetkit adopt` (adopt.py) with the fake engine: what is to adopt, the
report and its statuses, the refusals of --apply, the check after it, and that
`import` is only ever in a temporary program that is gone afterwards. Offline."""
import glob
import json
import os
import signal
import tempfile
import time
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
REPO = "github:index/repository:Repository"
SECRET = "github:index/actionsSecret:ActionsSecret"
FULL = {"liveFrom": "inputs", "declaredFrom": "declaration", "liveKnown": True, "declaredKnown": True, "kind": "real"}


def prog():
    return fakes.program(
        web={"type": CT, "properties": WEB},
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


def entry(report, key, stack="mini-guests"):
    return next(r for r in report["stacks"][stack]["resources"] if r["key"] == key)


def temp_dirs():
    return glob.glob(os.path.join(tempfile.gettempdir(), f"{adopt.TEMP_PREFIX}{os.getpid()}-*"))


def no_import_left(estate, w):
    """import was only ever in programs outside the state dir, those are gone,
    so is the run's own directory, and nothing ever called `pulumi cancel`."""
    with_import = [c for c in w.calls if c["imports"]]
    assert all(not c["wd"].startswith(str(estate.s.state_dir)) for c in with_import)
    assert all(Path(c["wd"]).name.startswith(adopt.TEMP_PREFIX) for c in with_import)
    assert all(not Path(c["wd"]).exists() for c in w.calls)
    assert estate.import_anywhere() == [] and estate.runs() == [] and temp_dirs() == []
    assert w.pulumi_cancel == 0
    # No up without an update plan, and it is the plan of the preview before it.
    for i, c in enumerate(w.calls):
        if c["verb"] == "up":
            assert c["plan"] and w.calls[i - 1]["verb"] == "preview" and w.calls[i - 1]["plan"] == c["plan"]
            assert w.calls[i - 1]["program"] == c["program"] and w.calls[i - 1]["targets"] == c["targets"]
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
    # In the program the engine ran, every guest is protected (guard.protect).
    assert call["protect"] == ["db", "old", "web"]
    text = "\n".join(adopt.text(report))
    assert "web: import pve1/101" in text and "old: cannot adopt: the guest has no vmid" in text
    assert "apps: already in state" in text and "nothing changed (pass --apply to adopt)" in text


def test_id_option_resolves_overrides_and_selects(lab):
    estate, w, p = lab
    w.live[(CT, "pve2/300")] = {"nodeName": "pve2", "vmId": 300}
    report = run(estate, ids={"old": "pve2/300"}, resources=["old", "apps"])
    assert statuses(report) == {"old": "import", "apps": "in-state"}
    assert no_import_left(estate, w)[0]["imports"] == {"old": "pve2/300"}
    # An id for a resource in state changes nothing: adopt does not touch it.
    n = len(w.calls)
    assert statuses(run(estate, ids={"apps": "other"}, resources=["apps"])) == {"apps": "in-state"}
    assert len(w.calls) == n
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
    web = entry(report, "web")
    assert web["status"] == "import+update"
    assert web["diff"] == [{"path": "cores", "live": 4, "declared": 2, **FULL}]
    # A difference is the report's content: ok, with what an apply would refuse.
    assert report["ok"] and [r["key"] for r in report["refused"]] == ["web"]
    assert "cores: live 4, declared 2" in "\n".join(adopt.text(report))
    no_import_left(estate, w)


def test_apply_adopts_and_verifies(lab):
    estate, w, p = lab
    events = []
    report = run(estate, events, apply=True)
    assert report["ok"] and report["applied"]
    assert report["stacks"]["mini-guests"]["verify"] == {"ok": True, "differs": [], "destroys": []}
    assert fakes.urn(p, "web") in w.state and fakes.urn(p, "db") in w.state
    web = w.state[fakes.urn(p, "web")]
    assert (web["id"], web["importID"], web["protect"]) == ("101", "pve1/101", True)
    assert w.updated == [] and w.created == [] and w.destroyed == []
    runs = str(estate.s.state_dir / "runs")
    assert [(c["verb"], bool(c["imports"]), c["wd"].startswith(runs)) for c in w.calls] == [
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
    assert w.calls == [] and estate.runs() == []


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


# ── F4: an id that is in state already, under another name ─────────────────
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("ids", [{"id": "101", "importID": "pve1/101"}, {"id": "101"}, {"id": "pve1/101"}])
def test_an_id_already_in_state_under_another_urn_is_refused(lab, apply, ids):
    """The key was renamed (web0 -> web): the guest is in state as web0. A
    second import would give the state two entries for it, and the next
    deploy would delete web0: the live guest."""
    estate, w, p = lab
    old = fakes.urn(p, "web").replace("::web", "::web0")
    w.state[old] = {"type": CT, "inputs": dict(WEB), **ids}
    report = run(estate, apply=apply)
    web = entry(report, "web")
    assert web["status"] == "duplicate" and web["twin"] == old
    assert "pulumi state rename" in web["why"] and old in web["why"] and "delete" in web["why"]
    assert not report["ok"] and not report["applied"]
    assert [r["kind"] for r in report["refused"] if r["key"] == "web"] == ["duplicate"]
    # It was never given to the engine with an import, and the state has one entry for the guest.
    assert all("web" not in c["imports"] for c in w.calls) and [c["verb"] for c in w.calls] == ["preview"]
    assert fakes.urn(p, "web") not in w.state and fakes.urn(p, "db") not in w.state
    assert "web: REFUSED" in "\n".join(adopt.text(report))
    no_import_left(estate, w)


def test_a_duplicate_of_another_type_or_another_guest_is_not_one(lab):
    estate, w, p = lab
    # The same id string on another type, and another vmid of the same type: not this resource.
    w.state["urn:pulumi:main::mini-guests::" + POOL + "::x"] = {"type": POOL, "id": "pve1/101", "inputs": {}}
    w.state["urn:pulumi:main::mini-guests::" + CT + "::y"] = {"type": CT, "id": "1101", "inputs": {}}
    assert statuses(run(estate))["web"] == "import"


def test_a_pool_already_in_state_under_another_key_is_refused(lab):
    estate, w, p = lab
    p["resources"]["apps2"] = {"type": POOL, "properties": {"poolId": "apps"},
                               "options": {"provider": "${provider-proxmox}"}}
    del p["resources"]["apps"]  # renamed apps -> apps2; the state still has apps
    estate.stack(p, adopt_ids={"apps2": "apps"})
    report = run(estate, apply=True)
    assert statuses(report) == {"apps2": "duplicate"} and not report["ok"] and w.calls == []
    # Found by the id it was imported by too, when the provider's own id is another.
    w.state[fakes.urn(prog(), "apps")].update(id="internal-7", importID="apps")
    assert statuses(run(estate, apply=True)) == {"apps2": "duplicate"} and w.calls == []


def test_verify_fails_when_the_real_preview_deletes_a_guest(lab):
    """Whatever the adopted URNs say: a delete or a replace of any guest in the
    preview after the adoption fails it."""
    estate, w, p = lab
    gone = "urn:pulumi:main::mini-guests::" + CT + "::gone"
    w.state[gone] = {"type": CT, "id": "777", "inputs": {}, "protect": True}
    report = run(estate, apply=True)
    v = report["stacks"]["mini-guests"]["verify"]
    assert report["applied"] and not report["ok"] and not v["ok"] and v["differs"] == []
    assert [(c["key"], c["op"]) for c in v["destroys"]] == [("gone", "delete")]
    assert "WOULD DELETE gone" in "\n".join(adopt.text(report)) and w.destroyed == []
    no_import_left(estate, w)


def test_verify_fails_when_the_real_preview_replaces_or_deletes_what_was_adopted(lab):
    estate, w, p = lab
    # A state entry of another type that carries the id that is adopted: the
    # real program does not have it, so its preview deletes it.
    twin = "urn:pulumi:main::mini-guests::proxmox:index/legacy:Legacy::legacy"
    w.state[twin] = {"type": "proxmox:index/legacy:Legacy", "id": "pve1/102", "inputs": {}}
    report = run(estate, apply=True)
    v = report["stacks"]["mini-guests"]["verify"]
    assert not v["ok"] and [(c["key"], c["op"]) for c in v["destroys"]] == [("legacy", "delete")]
    # A replacement of an adopted guest in the real preview: it differs, so not ok.
    w.state.pop(twin)
    w.state.pop(fakes.urn(p, "web"))
    w.state.pop(fakes.urn(p, "db"))
    w.calls.clear()

    def on_step(verb, op, key):  # the real program's preview (no import) replaces web
        if verb == "preview" and not w.calls[-1]["imports"]:
            w.force["web"] = {"steps": ["create-replacement", "replace", "delete-replaced"]}
    w.on_step = on_step
    report = run(estate, apply=True)
    v = report["stacks"]["mini-guests"]["verify"]
    assert not report["ok"] and [(c["key"], c["op"]) for c in v["differs"]] == [("web", "replace")]


# ── F6: signals, and what a kill leaves behind ─────────────────────────────
def test_stale_temporary_programs_are_swept_at_start(lab):
    estate, w, p = lab
    tmp = Path(tempfile.gettempdir())
    dead = tmp / f"{adopt.TEMP_PREFIX}999999999-mini-guests-dead"
    legacy_old = tmp / f"{adopt.TEMP_PREFIX}mini-guests-legacyold"
    legacy_new = tmp / f"{adopt.TEMP_PREFIX}mini-guests-legacynew"
    alive = tmp / f"{adopt.TEMP_PREFIX}{os.getppid()}-mini-guests-alive"
    try:
        for d in (dead, legacy_old, legacy_new, alive):
            d.mkdir()
            (d / "Pulumi.yaml").write_text('{"resources": {"web": {"options": {"import": "pve1/101"}}}}')
        os.utime(legacy_old, (time.time() - 7200, time.time() - 7200))
        run(estate)
        assert not dead.exists() and not legacy_old.exists()  # a dead run's, and an old one without a pid
        assert legacy_new.exists() and alive.exists()         # maybe someone's still running
    finally:
        for d in (dead, legacy_old, legacy_new, alive):
            if d.exists():
                (d / "Pulumi.yaml").unlink(missing_ok=True)
                d.rmdir()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_signal_during_the_up_is_an_orderly_exit(lab, monkeypatch, sig):
    """SIGTERM and SIGHUP used to kill the process where it stood, leaving the
    temporary program (with import) behind."""
    estate, w, p = lab
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}

    def on_step(verb, op, key):
        if verb == "up" and key == "web":
            assert temp_dirs()  # the temporary program is there now
            os.kill(os.getpid(), sig)
    w.on_step = on_step
    r = CliRunner().invoke(cli, ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir),
                                 "adopt", "mini", "--apply"])
    assert r.exit_code == 1 and "cancelled" in r.output and not isinstance(r.exception, KeyboardInterrupt)
    # The engine was signalled, not `pulumi cancel`led: the step in flight
    # finished, the next one did not start, and the message says so.
    assert w.stops == ["soft"] and w.pulumi_cancel == 0
    assert fakes.urn(p, "web") in w.state and fakes.urn(p, "db") not in w.state
    assert "Stopped after import web" in r.output and "may be partly applied" in r.output
    no_import_left(estate, w)
    assert {s: signal.getsignal(s) for s in before} == before


# ── R1: both sides of every difference ─────────────────────────────────────
def repos(**live):
    p = fakes.program(**{k: {"type": REPO, "properties": {"name": k}} for k in live})
    p["name"] = "mini-github"
    return p


def test_a_false_live_value_is_reported_with_both_sides(estate):
    """What an import read leaves zero values (false, 0, "") out of the
    inputs; the entry used to come out with neither side."""
    p = repos(r1={})
    w = estate.stack(p, adopt_ids={"r1": "r1"})
    w.defaults[REPO] = {"allowRebaseMerge": True, "visibility": "private", "topics": ["x"], "size": 5}
    w.live[(REPO, "r1")] = {"name": "r1", "allowRebaseMerge": False, "visibility": "", "topics": [], "size": 0}
    report = run(estate)
    r = entry(report, "r1", "mini-github")
    assert r["status"] == "import+update"
    diff = {d["path"]: d for d in r["diff"]}
    assert sorted(diff) == ["allowRebaseMerge", "size", "topics", "visibility"]
    for path, live, declared in (("allowRebaseMerge", False, True), ("visibility", "", "private"), ("size", 0, 5)):
        d = diff[path]
        assert d["liveKnown"] and d["declaredKnown"] and d["live"] == live and d["live"] is not None
        assert (d["liveFrom"], d["declared"], d["declaredFrom"], d["kind"]) == ("state", declared, "provider default", "real")
    assert diff["topics"]["live"] == [] and diff["topics"]["liveFrom"] == "inputs"
    text = "\n".join(adopt.text(report))
    assert "allowRebaseMerge: live false (state), declared true (provider default)" in text
    assert 'visibility: live "" (state), declared "private" (provider default)' in text


# ── R2: a resource that does not exist yet ─────────────────────────────────
def test_an_absent_resource_is_not_an_error_and_the_rest_is_complete(estate, monkeypatch):
    """r1 does not exist: the engine stops at its import, before it compared
    r2 and r3. They used to be reported as a clean import; and the run failed."""
    p = repos(r1={}, r2={}, r3={}, r4={})
    w = estate.stack(p, adopt_ids={k: k for k in ("r1", "r2", "r3", "r4")})
    w.defaults[REPO] = {"description": ""}
    for k in ("r2", "r3", "r4"):
        w.live[(REPO, k)] = {"name": k, "description": f"what {k} is"}
    w.live[(REPO, "r4")]["description"] = ""
    report = run(estate)
    assert statuses(report, "mini-github") == {"r1": "absent", "r2": "import+update", "r3": "import+update",
                                               "r4": "import"}
    r1 = entry(report, "r1", "mini-github")
    assert "does not exist" in r1["note"] and "a deploy will create it" in r1["note"] and "'r1' does not exist" in r1["detail"]
    assert entry(report, "r2", "mini-github")["diff"][0]["live"] == "what r2 is"
    assert report["ok"] and all(r["key"] != "r1" for r in report["refused"])
    assert "error" not in report["stacks"]["mini-github"]
    # The preview was run again without it, and that one carried no import for it.
    assert [sorted(c["imports"]) for c in w.calls] == [["r1", "r2", "r3", "r4"], ["r2", "r3", "r4"]]
    assert "r1: absent" in "\n".join(adopt.text(report))
    # With --apply the rest is adopted; the absent one is left for a deploy to create. Exit 0.
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    r = CliRunner().invoke(cli, ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir), "adopt",
                                 "mini", "--apply", "--accept-update", "r2", "--accept-update", "r3"])
    assert r.exit_code == 0, r.output
    assert sorted(u.rsplit("::", 1)[1] for u in w.state) == ["provider-proxmox", "r2", "r3", "r4"]
    assert w.calls[-2]["verb"] == "up" and sorted(w.calls[-2]["imports"]) == ["r2", "r3", "r4"]
    no_import_left(estate, w)


def test_a_real_import_failure_is_an_error_and_the_rest_is_still_complete(estate):
    p = repos(r1={}, r2={}, r3={})
    w = estate.stack(p, adopt_ids={k: k for k in ("r1", "r2", "r3")})
    for k in ("r1", "r2", "r3"):
        w.live[(REPO, k)] = {"name": k}
    w.import_error[(REPO, "r2")] = "GET https://api.github.example/repos/o/r2: 401 Bad credentials"
    report = run(estate, apply=True)
    assert statuses(report, "mini-github") == {"r1": "import", "r2": "error", "r3": "import"}
    assert "401 Bad credentials" in entry(report, "r2", "mini-github")["error"]
    assert not report["ok"] and not report["applied"] and [c["verb"] for c in w.calls] == ["preview", "preview"]


def test_an_import_the_engine_only_started_is_never_reported_clean(estate):
    """Only the pre event of an import, no outputs event: not compared."""
    p = repos(r1={}, r2={})
    w = estate.stack(p, adopt_ids={"r1": "r1", "r2": "r2"})
    w.live[(REPO, "r1")] = {"name": "r1"}
    w.live[(REPO, "r2")] = {"name": "r2"}
    w.stop_after = None
    w.fail["preview"] = RuntimeError("provider crashed")
    orig = fakes.FakeStack._run

    def run_and_drop_outputs(self, verb, kw):
        on_event = kw["on_event"]
        kw = {**kw, "on_event": lambda e: None if e.res_outputs_event else on_event(e)}
        return orig(self, verb, kw)
    fakes.FakeStack._run = run_and_drop_outputs
    try:
        report = run(estate)
    finally:
        fakes.FakeStack._run = orig
    assert statuses(report, "mini-github") == {"r1": "error", "r2": "error"} and not report["ok"]
    assert "provider crashed" in entry(report, "r1", "mini-github")["error"]


# ── R3: what the import does not record ────────────────────────────────────
def test_unrecorded_properties_are_their_own_status_and_still_need_accepting(lab):
    """cpu, memory, vmId (...) of a bpg guest are not recorded by its import:
    the engine plans an update for them although nothing differs. For a
    container that update is a reboot."""
    estate, w, p = lab
    p["resources"]["web"]["properties"] = {**WEB, "cpu": {"cores": 2}, "memory": {"dedicated": 512}}
    estate.stack(p, adopt_ids={"web": "pve1/101"})
    w.live[(CT, "pve1/101")] = {**WEB, "cpu": {"cores": 2}, "memory": {"dedicated": 512}}
    w.unrecorded[(CT, "pve1/101")] = ["cpu", "memory", "vmId"]
    report = run(estate, apply=True)
    web = entry(report, "web")
    assert web["status"] == "import+unrecorded"
    assert [(d["path"], d["kind"], d["liveKnown"], "live" in d) for d in web["diff"]] == [
        ("cpu", "unrecorded", False, False), ("memory", "unrecorded", False, False),
        ("vmId", "unrecorded", False, False)]
    assert web["diff"][0]["declared"] == {"cores": 2}
    (r,) = report["refused"]
    assert not report["applied"] and r["kind"] == "update" and "REBOOTS" in r["why"]
    assert "does not record cpu, memory, vmId" in r["why"] and "--accept-update web" in r["why"]
    text = "\n".join(adopt.text(report))
    assert "it REBOOTS the guest" in text and "cpu: live not recorded by the import, declared" in text
    assert "[unrecorded]" in text
    # Accepted: adopted, with the update.
    report = run(estate, apply=True, accept_update=["web"])
    assert report["applied"] and report["ok"] and w.updated == ["web"]
    no_import_left(estate, w)


def test_a_real_difference_beside_an_unrecorded_one_is_an_update(lab):
    estate, w, p = lab
    w.live[(CT, "pve1/101")]["cores"] = 4
    w.unrecorded[(CT, "pve1/101")] = ["vmId"]
    web = entry(run(estate), "web")
    assert web["status"] == "import+update"
    assert {d["path"]: d["kind"] for d in web["diff"]} == {"cores": "real", "vmId": "unrecorded"}
    # A pool whose only differences are unrecorded: the same status, no reboot in the words.
    p["resources"]["net"] = {"type": POOL, "properties": {"poolId": "net", "comment": "c"},
                             "options": {"provider": "${provider-proxmox}"}}
    estate.stack(p, adopt_ids={"net": "net"})
    w.live[(POOL, "net")] = {"poolId": "net", "comment": "c"}
    w.unrecorded[(POOL, "net")] = ["comment"]
    report = run(estate, apply=True)
    assert statuses(report) == {"net": "import+unrecorded"}
    assert "REBOOTS" not in report["refused"][0]["why"] and "--accept-update net" in report["refused"][0]["why"]


# ── R4: secrets ────────────────────────────────────────────────────────────
def test_a_secret_is_not_adopted_unless_named(estate):
    p = repos(r1={})
    p["resources"]["tok"] = {"type": SECRET, "options": {"provider": "${provider-proxmox}"},
                             "properties": {"repository": "r1", "secretName": "TOK", "plaintextValue": "v4lue-of-it"}}
    w = estate.stack(p, adopt_ids={"r1": "r1", "tok": "r1:TOK"})
    w.live[(REPO, "r1")] = {"name": "r1"}
    w.live[(SECRET, "r1:TOK")] = {"repository": "r1", "secretName": "TOK"}  # the value is never read back
    report = run(estate, apply=True)
    assert statuses(report, "mini-github") == {"r1": "import", "tok": "secret"}
    tok = entry(report, "tok", "mini-github")
    assert "--resource tok" in tok["note"] and "cannot be read back" in tok["note"]
    assert report["ok"] and report["applied"] and report["refused"] == []
    assert all("tok" not in c["imports"] for c in w.calls) and fakes.urn(p, "tok") not in w.state
    assert "tok: secret, not adopted" in "\n".join(adopt.text(report))
    # Named: its own status, adopted, the declared value written; naming it is the acceptance.
    report = run(estate, resources=["tok"])
    assert statuses(report, "mini-github") == {"tok": "import+secret"} and report["refused"] == []
    report = run(estate, resources=["tok"], apply=True)
    assert report["applied"] and report["ok"] and fakes.urn(p, "tok") in w.state and w.updated == ["tok"]
    assert "write of the secret" in "\n".join(adopt.text(report))
    no_import_left(estate, w)


def test_a_key_that_two_stacks_have_is_refused_as_ambiguous(lab):
    estate, w, p = lab
    more = {**prog(), "name": "mini-more"}
    w2 = estate.stack(more, adopt_ids={"web": "pve1/101"})
    w2.live[(CT, "pve1/101")] = {**WEB, "cores": 8}
    for kw in ({"accept_update": ["web"]}, {"resources": ["web"]}, {"ids": {"web": "pve1/101"}}):
        with pytest.raises(adopt.AdoptError, match="web is ambiguous: web is a resource of mini-guests, mini-more"):
            run(estate, apply=True, **kw)
    assert w.calls == [] and w2.calls == []
    report = run(estate, apply=True, stacks=["mini-more"], accept_update=["web"])
    assert report["applied"] and w2.updated == ["web"]
    # The stack that was not selected: read, and previewed once after the
    # adoption (does it delete what holds the adopted id?), nothing else.
    assert [(c["verb"], c["imports"]) for c in w.calls] == [("preview", {})]
    assert report["stacks"]["mini-guests"]["verify"] == {"ok": True, "differs": [], "destroys": [],
                                                         "scope": "adopted ids"}
