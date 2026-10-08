"""The guard every `up` passes (guard.py, infra.py, pipeline.py), with the
fake engine: a plan that replaces, deletes or updates a guest is refused
unless the guest is named for that op; other resources are listed and let
through; a program that carries `import` is refused; summaries name
resources; every up is bound to the plan of its preview, in a directory of
its own, with the unnamed guests protected; a cancel signals the engine and
never calls `pulumi cancel`. Offline."""
import json
import threading
import time

import pytest
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

import fakes
from fakes import CT, POOL, VM
from fleetkit_cli import guard, infra, pipeline, render
from fleetkit_cli.api import create_app
from fleetkit_cli.events import Emitter
from fleetkit_cli.gitops import _comment
from fleetkit_cli.jobs import JobManager
from fleetkit_cli.main import cli
from fleetkit_cli.pipeline import DeployRequest

REPLACE = ["create-replacement", "replace", "delete-replaced"]


def guests(**over):
    return fakes.program(
        web={"type": CT, "properties": {"nodeName": "pve1", "vmId": 101, "cores": 2}},
        db={"type": VM, "properties": {"nodeName": "pve1", "vmId": 102}},
        apps={"type": POOL, "properties": {"poolId": "apps"}}, **over)


def deploy(estate, events=None, **req):
    ev = Emitter((events if events is not None else []).append)
    return pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False, **req), ev)


def ups(world):
    return [c for c in world.calls if c["verb"] == "up"]


@pytest.fixture
def settled(estate):
    """Everything of the program in state, nothing to do."""
    prog = guests()
    w = estate.stack(prog)
    for k in prog["resources"]:
        w.in_state(prog, k)
    return estate, w, prog


def bound(world):
    """Every up ran the plan of the preview before it, on the same program."""
    for i, c in enumerate(world.calls):
        if c["verb"] == "up":
            before = world.calls[i - 1]
            assert c["plan"] and before["verb"] == "preview" and before["plan"] == c["plan"]
            assert (before["program"], before["wd"], before["targets"]) == (c["program"], c["wd"], c["targets"])
    assert world.pulumi_cancel == 0
    return True


def test_guest_types_are_the_proxmox_containers_and_vms():
    assert {CT, VM} < guard.GUEST_TYPES and len(guard.GUEST_TYPES) == 6
    assert all(t.startswith("proxmox:index/") for t in guard.GUEST_TYPES) and POOL not in guard.GUEST_TYPES


@pytest.mark.parametrize("key", ["web", "db"])
@pytest.mark.parametrize("steps,fam", [
    (REPLACE, "replace"), (["replace"], "replace"), (["create-replacement"], "replace"),
    (["delete-replaced"], "replace"), (["update"], "update")])
def test_gated_op_on_a_guest_is_refused_unless_named(settled, key, steps, fam):
    estate, w, _ = settled
    w.force[key] = {"steps": steps, "diff": ["cores"], "keys": ["vmId"] if fam == "replace" else None}
    with pytest.raises(guard.GuardError) as e:
        deploy(estate)
    assert ups(w) == [] and w.destroyed == [] and w.updated == []
    assert f"{fam} of guest {key}" in str(e.value) and f"--allow-{fam} {key}" in str(e.value)
    (r,) = e.value.result["refused"]["mini-guests"]
    assert (r["key"], r["op"], r["flag"]) == (key, fam, f"--allow-{fam} {key}")
    # Named for another op, or another guest named: still refused.
    other = next(f for f in ("replace", "update", "delete") if f != fam)
    for wrong in ({f"allow_{other}": [key]}, {f"allow_{fam}": ["web" if key == "db" else "db"]}):
        with pytest.raises(guard.GuardError):
            deploy(estate, **wrong)
    assert ups(w) == []
    # Named for this op: it runs.
    out = deploy(estate, **{f"allow_{fam}": [key]})
    assert len(ups(w)) == 1 and out["refused"] == {} and bound(w)
    assert [c["key"] for c in out["plan"]["mini-guests"]] == [key]
    assert estate.runs() == []  # the run's directory is gone


def test_delete_of_a_guest_is_refused_unless_named(settled):
    estate, w, prog = settled
    del prog["resources"]["web"]
    estate.stack(prog)  # the program no longer has web; the state does
    with pytest.raises(guard.GuardError, match="delete of guest web .*--allow-delete web"):
        deploy(estate)
    assert ups(w) == [] and w.destroyed == []
    with pytest.raises(guard.GuardError):
        deploy(estate, allow_replace=["web"], allow_update=["web"])
    deploy(estate, allow_delete=["web"])
    assert w.destroyed == ["web"]


def test_refusal_names_every_offending_resource_and_applies_nothing(settled):
    estate, w, _ = settled
    w.force["web"] = {"steps": REPLACE, "keys": ["vmId"]}
    w.force["db"] = {"steps": ["update"], "diff": ["memory"]}
    w.force["apps"] = {"steps": ["update"], "diff": ["comment"]}
    with pytest.raises(guard.GuardError) as e:
        deploy(estate, allow_replace=["web"])
    assert "update of guest db" in str(e.value) and "web" not in str(e.value)
    assert "reboots" in str(e.value) and "nothing was applied" in str(e.value)
    assert ups(w) == []
    plan = {c["key"]: c for c in e.value.result["plan"]["mini-guests"]}
    assert plan["web"]["op"] == "replace" and plan["web"]["steps"] == REPLACE
    assert plan["web"]["replaceReasons"] == ["vmId"] and plan["db"]["diff"] == ["memory"]
    assert plan["web"]["urn"].endswith(f"::{CT}::web") and plan["apps"]["type"] == POOL


def test_non_guest_replace_is_listed_and_allowed(settled):
    estate, w, _ = settled
    w.force["apps"] = {"steps": REPLACE, "keys": ["poolId"]}
    events = []
    out = deploy(estate, events)
    assert len(ups(w)) == 1 and out["refused"] == {}
    assert [(c["key"], c["op"], c["type"]) for c in out["plan"]["mini-guests"]] == [("apps", "replace", POOL)]
    plan = next(e for e in events if e["kind"] == "plan")
    assert any("replace" in line and "apps" in line for line in plan["text"])


def test_create_of_a_guest_is_not_gated(estate):
    w = estate.stack(guests())
    out = deploy(estate)
    assert sorted(w.created) == ["apps", "db", "provider-proxmox", "web"] and out["refused"] == {}
    assert out["infra"]["mini-guests"] == {"create": 4}


def test_program_with_import_is_refused(settled):
    estate, w, prog = settled
    prog["resources"]["web"]["options"]["import"] = "pve1/101"
    estate.stack(prog)
    with pytest.raises(guard.GuardError) as e:
        deploy(estate)
    assert ups(w) == []
    assert "import on web" in str(e.value) and "issue #61" in str(e.value) and "fleetkit adopt" in str(e.value)
    assert e.value.result["refused"]["mini-guests"][0]["op"] == "import"
    # No flag lets it through.
    with pytest.raises(guard.GuardError):
        deploy(estate, allow_replace=["web"], allow_update=["web"], allow_delete=["web"])
    assert ups(w) == []
    # A preview says so, and succeeds.
    out = deploy(estate, preview=True)
    assert out["refused"]["mini-guests"][0]["key"] == "web"


def test_summary_and_preview_list_resources_by_op(settled):
    estate, w, _ = settled
    w.force["web"] = {"steps": REPLACE, "keys": ["vmId"]}
    w.force["apps"] = {"steps": ["update"], "diff": ["comment"]}
    events = []
    out = deploy(estate, events, preview=True)
    assert w.calls and ups(w) == []
    summary = next(e for e in events if e["kind"] == "summary")
    assert summary["changes"] == {"create-replacement": 1, "replace": 1, "delete-replaced": 1, "update": 1,
                                  "same": 2}
    assert summary["resources"] == {"replace": [{"key": "web", "type": CT}],
                                    "update": [{"key": "apps", "type": POOL}]}
    assert [r["flag"] for r in summary["refused"]] == ["--allow-replace web"]
    assert [r["key"] for r in out["refused"]["mini-guests"]] == ["web"]
    text = "\n".join(next(e for e in events if e["kind"] == "plan")["text"])
    assert "a deploy would refuse: replace of guest web" in text and "needs --allow-replace web" in text
    # Named, a preview no longer says it would be refused.
    assert deploy(estate, preview=True, allow_replace=["web"])["refused"] == {}


def test_second_stack_refused_leaves_the_first_unapplied(estate):
    a = fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}})
    b = {**guests(), "name": "mini-more"}
    wa, wb = estate.stack(a), estate.stack(b)
    for k in b["resources"]:
        wb.in_state(b, k)
    wb.force["web"] = {"steps": ["update"], "diff": ["cores"]}
    with pytest.raises(guard.GuardError, match="mini-more: update of guest web"):
        deploy(estate)
    assert ups(wa) == [] and wa.created == [] and ups(wb) == []


def test_cli_lists_the_plan_and_fails_a_refused_deploy(settled, monkeypatch):
    estate, w, _ = settled
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    w.force["db"] = {"steps": REPLACE, "keys": ["nodeName"]}
    base = ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir)]
    r = CliRunner().invoke(cli, [*base, "preview", "mini", "--no-nixos"])
    assert r.exit_code == 0 and "replace  db" in r.output and "a deploy would refuse" in r.output
    r = CliRunner().invoke(cli, [*base, "deploy", "mini", "--no-nixos"])
    assert r.exit_code == 1 and "needs --allow-replace db" in r.output and ups(w) == []
    r = CliRunner().invoke(cli, [*base, "deploy", "mini", "--no-nixos", "--allow-replace", "db"])
    assert r.exit_code == 0 and len(ups(w)) == 1 and w.destroyed == ["db"]


def test_api_refused_deploy_is_a_failed_job_carrying_the_plan(settled, tmp_path):
    estate, w, _ = settled
    w.force["web"] = {"steps": REPLACE, "keys": ["vmId"]}
    m = JobManager(tmp_path / "jobs", lambda req, ev: pipeline.run(estate.s, req, ev))
    c = TestClient(create_app(m, estate.s, None))

    def run(body):
        jid = c.post("/v1/deploys", json={"estate": "mini", "nixos": False, **body}).json()["id"]
        for _ in range(500):
            rec = c.get(f"/v1/deploys/{jid}").json()
            if rec["state"] in ("succeeded", "failed"):
                return rec, m.jobs[jid]
            time.sleep(0.01)
        raise AssertionError(rec)

    rec, job = run({})
    assert rec["state"] == "failed" and rec["error"].startswith("GuardError: refused, nothing was applied")
    assert rec["result"]["refused"]["mini-guests"][0]["key"] == "web"
    assert rec["result"]["plan"]["mini-guests"][0]["op"] == "replace" and ups(w) == []
    # What a pull request shows of it (gitops.py).
    link = {"kind": "deploy", "estate": "mini", "sha": "0123456789abcdef", "pr": 1, "repo": "o/r"}
    body = _comment(job, link, None, merge_deploys=True)
    assert "- replace: `web`" in body and "refused: replace of guest web" in body and "--allow-replace web" in body
    rec, job = run({"preview": True})
    assert rec["state"] == "succeeded"
    assert "a deploy would refuse: replace of guest web" in _comment(job, {**link, "kind": "preview"}, None, True)
    rec, _ = run({"allow_replace": ["web"]})
    assert rec["state"] == "succeeded" and w.destroyed == ["web"]
    assert c.post("/v1/deploys", json={"estate": "mini", "allow_replace": "web"}).status_code == 422


def test_render_reads_adoption_ids_with_a_default(monkeypatch):
    assert "adoptIds = x.adoptIds or { }" in render.VIEW and "adoptUnresolved = x.adoptUnresolved or { }" in render.VIEW
    base = {"estate": "mini", "project": "p", "backend": None, "file": "/f", "secrets": []}
    monkeypatch.setattr(render, "nix_eval_json", lambda s, attr, apply=None: {
        "old": dict(base), "new": {**base, "adoptIds": {"web": "pve1/101"}, "adoptUnresolved": {"db": "no vmid"}}})
    out = render.stacks(None)
    assert out["old"]["adoptIds"] == {} and out["old"]["adoptUnresolved"] == {}
    assert out["new"]["adoptIds"] == {"web": "pve1/101"} and out["new"]["adoptUnresolved"] == {"db": "no vmid"}


def test_plan_helpers():
    prog = guests()
    prog["resources"]["child"] = {"type": POOL, "properties": {"poolId": "${web.vmId}"},
                                  "options": {"parent": "${apps}", "dependsOn": ["${db}"]}}
    assert guard.urn_of("main", prog, "child") == f"urn:pulumi:main::mini-guests::{POOL}${POOL}::child"
    assert guard.depends_on(prog, "child") == {"apps", "db", "web", "provider-proxmox"}
    assert guard.depends_on(prog, "web") == {"provider-proxmox"}
    assert guard.lookup({"a": [{"b": 1}], "c": {"d.e": 2}}, "a[0].b") == (True, 1)
    assert guard.lookup({"c": {"d.e": 2}}, 'c["d.e"]') == (True, 2) and guard.lookup({}, "x")[0] is False
    assert guard.scrub({"t": {guard.SECRET_SIG: "1b47", "value": "hunter2"}, "u": [1]}) == {"t": "[secret]", "u": [1]}
    assert json.loads(json.dumps(guard.imports(prog))) == []


# ── F1: the up is bound to the plan; cancel signals the engine; protect ────
def test_an_up_that_leaves_its_plan_is_refused_by_the_engine(settled):
    """The world moved between the preview and the up (the program, the
    state): the engine itself refuses, before any step of that resource."""
    estate, w, _ = settled
    w.drift["web"] = {"steps": REPLACE}
    events = []
    with pytest.raises(guard.GuardError, match="refused by the engine: it left the plan") as e:
        deploy(estate, events)
    assert w.destroyed == [] and w.stops == [] and bound(w)
    info = e.value.result["stopped"]
    assert info["completed"] == [] and info["in_flight"] == [] and not info["partly_applied"]
    assert "Nothing was applied" in str(e.value) and e.value.result["plan"]["mini-guests"] == []
    # The engine prints the values of the properties that left the plan; a
    # secret would be among them. Neither the error nor an event carries them.
    assert fakes.SECRET_VALUE not in str(e.value) and fakes.SECRET_VALUE not in json.dumps(events)
    assert "=~cores [values withheld]" in str(e.value)


def test_no_up_without_a_plan(settled):
    estate, w, _ = settled
    ev = Emitter(lambda e: None)
    with render.Run(estate.s, "deploy") as rundir:
        project = render.render(estate.s, "mini-guests", estate.stacks["mini-guests"], ev, rundir)
        st = infra.open_stack(estate.s, project.wd, "mini-guests", {}, ev)
        with pytest.raises(guard.GuardError, match="without an update plan is never run"):
            infra.engine(st, ev, "mini-guests", "up", guard.Plan("main", {"name": "x", "resources": {}}))
        for planned in ({}, {"bound": None, "plan": [], "refused": []}):
            with pytest.raises(guard.GuardError, match="an up without one is never run"):
                infra.apply(estate.s, project.wd, "mini-guests", {}, ev, planned)
        # A plan made with other targets, or with a refusal in it, is not applied either.
        planned = infra.plan(estate.s, project.wd, "mini-guests", {}, ev, False)
        with pytest.raises(guard.GuardError, match="made for another run"):
            infra.apply(estate.s, project.wd, "mini-guests", {}, ev, planned, targets=["urn:x"])
        with pytest.raises(guard.GuardError, match="refused"):
            infra.apply(estate.s, project.wd, "mini-guests", {}, ev,
                        {**planned, "refused": [{"key": "web", "type": CT, "op": "replace", "flag": "--allow-replace web"}]})
    assert ups(w) == []


def test_unnamed_guests_are_protected_in_the_program_that_runs(settled):
    """Defence in depth: a copy of the program, never the estate's file."""
    estate, w, prog = settled
    w.force["web"] = {"steps": REPLACE}
    store = estate.stacks["mini-guests"]["file"]
    before = open(store).read()
    deploy(estate, allow_replace=["web"])
    preview, up = w.calls
    assert preview["protect"] == ["db"] and up["protect"] == ["db"]  # web is named for a replace
    assert open(store).read() == before and '"protect"' not in before
    # After the up the state protects what the run protected.
    assert w.state[fakes.urn(prog, "db")]["protect"] is True and w.state[fakes.urn(prog, "web")]["protect"] is False
    w.force.clear()
    w.calls.clear()
    deploy(estate, allow_update=["web"], allow_delete=["db"])  # an update needs no unprotecting
    assert w.calls[-1]["protect"] == ["web"]


def test_protect_alone_stops_an_unplanned_replace(settled):
    """With the plan layer switched off, the engine still refuses."""
    estate, w, prog = settled
    deploy(estate)  # one deploy: the state now protects web and db
    w.ignore_plan = True
    w.drift["web"] = {"steps": REPLACE}
    with pytest.raises(infra.EngineError, match="marked for protection") as e:
        deploy(estate)
    assert w.destroyed == [] and w.stops == [] and "Nothing was applied" in str(e.value)


def test_tripwire_stops_the_engine_by_signal_when_both_other_layers_are_off(settled):
    estate, w, _ = settled
    w.ignore_plan = w.ignore_protect = True
    w.drift["web"] = {"steps": REPLACE}
    with pytest.raises(guard.GuardError, match="started a step its plan did not have.*create-replacement of guest web") as e:
        deploy(estate)
    assert w.stops == ["hard"] and w.pulumi_cancel == 0 and w.destroyed == []
    info = e.value.result["stopped"]
    assert info["in_flight"] == [{"op": "create-replacement", "key": "web"}] and info["partly_applied"]
    assert "whether they happened is not known" in str(e.value) and "terminate at once" in str(e.value)


def test_estate_protect_is_never_removed(settled):
    """A guest the estate protects stays protected although it is named."""
    estate, w, prog = settled
    prog["resources"]["web"]["options"]["protect"] = True
    estate.stack(prog)
    w.force["web"] = {"steps": REPLACE}
    with pytest.raises(infra.EngineError, match="marked for protection"):
        deploy(estate, allow_replace=["web"])
    assert ups(w) == [] and w.destroyed == []


def test_named_delete_of_a_guest_the_state_protects(settled):
    estate, w, prog = settled
    deploy(estate)  # the state now protects web
    assert w.state[fakes.urn(prog, "web")]["protect"] is True
    web = fakes.urn(prog, "web")
    del prog["resources"]["web"]
    estate.stack(prog)
    w.calls.clear()
    # Not named: refused by name, although the engine's preview fails on protect.
    with pytest.raises(guard.GuardError, match="delete of guest web .*--allow-delete web"):
        deploy(estate)
    out = deploy(estate, preview=True)
    assert [(c["key"], c["op"], c.get("protected")) for c in out["plan"]["mini-guests"]] == [("web", "delete", True)]
    assert out["refused"]["mini-guests"][0]["flag"] == "--allow-delete web"
    # Named in a preview: listed, not refused, and nothing is unprotected.
    out = deploy(estate, preview=True, allow_delete=["web"])
    assert out["refused"] == {} and w.unprotected == [] and ups(w) == [] and w.destroyed == []
    # Named in a deploy: unprotected in state, planned again, applied.
    out = deploy(estate, allow_delete=["web"])
    assert w.unprotected == [web] and w.destroyed == ["web"] and bound(w) and out["applied"] == ["mini-guests"]


def test_api_cancel_signals_the_engine_and_says_what_was_done(estate, tmp_path):
    """`pulumi cancel` only removed the lock and the engine ran on. A cancel
    now signals the engine: the step in flight finishes, no other starts."""
    w = estate.stack(guests())
    reached, go = threading.Event(), threading.Event()

    def on_step(verb, op, key):
        if verb == "up" and key == "web":
            reached.set()
            assert go.wait(10)
    w.on_step = on_step
    m = JobManager(tmp_path / "jobs", lambda req, ev: pipeline.run(estate.s, req, ev))
    c = TestClient(create_app(m, estate.s, None))
    jid = c.post("/v1/deploys", json={"estate": "mini", "nixos": False}).json()["id"]
    assert reached.wait(10)
    c.post(f"/v1/deploys/{jid}/cancel")
    go.set()
    for _ in range(500):
        rec = c.get(f"/v1/deploys/{jid}").json()
        if rec["state"] not in ("queued", "running"):
            break
        time.sleep(0.01)
    assert rec["state"] == "cancelled" and w.stops == ["soft"] and w.pulumi_cancel == 0
    stopped = rec["result"]["stopped"]
    assert stopped["completed"] == [{"op": "create", "key": "provider-proxmox"}, {"op": "create", "key": "web"}]
    assert stopped["in_flight"] == [] and stopped["partly_applied"] and stopped["signals"] == ["SIGINT"]
    assert "Stopped after create provider-proxmox, create web (2 step(s) completed)" in rec["error"]
    assert "may be partly applied" in rec["error"] and "stop after the step in flight" in rec["error"]
    assert sorted(w.created) == ["provider-proxmox", "web"]  # db and apps were never started
    assert rec["result"]["plan"]["mini-guests"] and estate.runs() == []


def test_a_second_cancel_terminates_at_once(estate):
    w = estate.stack(guests())
    ev = Emitter(lambda e: None)

    def on_step(verb, op, key):
        if verb == "up" and key == "web":
            ev.cancel()
            ev.cancel()
    w.on_step = on_step
    with pytest.raises(pipeline.Cancelled) as e:
        pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False), ev)
    assert w.stops == ["soft", "hard"] and w.pulumi_cancel == 0
    info = e.value.result["stopped"]
    assert info["in_flight"] == [{"op": "create", "key": "web"}] and "whether they happened is not known" in str(e.value)


def test_cancel_during_a_preview_says_nothing_changed(estate):
    w = estate.stack(guests())
    ev = Emitter(lambda e: None)
    w.on_step = lambda verb, op, key: ev.cancel() if key == "web" else None
    with pytest.raises(pipeline.Cancelled, match="A preview changes nothing"):
        pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False, preview=True), ev)
    assert w.created == [] and w.pulumi_cancel == 0


def test_violation_values_are_withheld_and_the_sdk_detailed_diff_is_read():
    line = ("error: resource urn:pulumi:main::t::random:index/randomString:RandomString::k violates plan: "
            "properties changed: =~keepers[{map[p:{&{{DayrixX0Jp2u}}} t:{&{{[secret]}}}]}], ++length[{9}]")
    out = infra.scrub_violation(f"x\n{line}\ny")
    assert "DayrixX0Jp2u" not in out and out.splitlines()[0] == "x" and out.splitlines()[2] == "y"
    assert out.splitlines()[1].endswith("properties changed: =~keepers, ++length [values withheld]")
    # The engine writes `detailedDiff`; the pinned SDK reads `detailed_diff`.
    from pulumi import automation as auto
    raw = {"sequence": 1, "timestamp": 1, "resourcePreEvent": {"metadata": {
        "op": "replace", "urn": "urn:pulumi:main::p::t:index/a:A::k", "type": "t:index/a:A", "diffs": ["a"],
        "detailedDiff": {"a.b": {"diffKind": "update-replace", "inputDiff": True}, "c": {"diffKind": "add"}}}}}
    m = auto.EngineEvent.from_json(raw).resource_pre_event.metadata
    assert m.detailed_diff == raw["resourcePreEvent"]["metadata"]["detailedDiff"]
    p = guard.Plan("main", {"name": "p", "resources": {"k": {"type": "t:index/a:A"}}})
    entry = p.step(m)
    assert entry["diff"] == ["a.b", "c"] and entry["replaceReasons"] == ["a.b"] and entry["kinds"]["c"] == "add"


# ── F2: a directory per run ────────────────────────────────────────────────
def test_another_render_between_plan_and_up_changes_nothing(estate, monkeypatch):
    """The work dir used to be keyed by stack: a preview (or an adoption, or
    `fleetkit render`) of another commit, between this deploy's plan and its
    up, repointed the program, and the deploy applied what it never planned."""
    v1 = fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}})
    v2 = fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}},
                       evil={"type": POOL, "properties": {"poolId": "evil"}})
    w = estate.stack(v1)
    real_apply, others = infra.apply, []

    def apply(s, wd, stack, env, ev, planned, *a, **kw):
        if not others:
            # Before the up: the estate moves on, and others render it.
            estate.stack(v2)
            ev2 = Emitter(lambda e: None)
            others.append(deploy(estate, preview=True))
            others.append(render.render(estate.s, "mini-guests", estate.stacks["mini-guests"], ev2,
                                        render.Run(estate.s, "render", keep=True)))
        return real_apply(s, wd, stack, env, ev, planned, *a, **kw)
    monkeypatch.setattr(infra, "apply", apply)
    out = deploy(estate)
    first, other, up = w.calls
    assert (first["verb"], other["verb"], up["verb"]) == ("preview", "preview", "up")
    assert up["program"] == first["program"] != other["program"] and up["wd"] == first["wd"] != other["wd"]
    assert sorted(w.created) == ["apps", "provider-proxmox"] and up["plan"] == first["plan"] != other["plan"]
    assert out["programs"]["mini-guests"].endswith("mini-guests-Pulumi.yaml") and len(out["program_sha256"]["mini-guests"]) == 64
    assert others[1].wd.parent != Path(first["wd"]).parent and others[1].sha256 != out["program_sha256"]["mini-guests"]


@pytest.mark.parametrize("what", ["program", "plan"])
def test_a_program_or_plan_changed_after_the_plan_is_refused(estate, monkeypatch, what):
    prog = fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}})
    w = estate.stack(prog)
    real_apply = infra.apply

    def apply(s, wd, stack, env, ev, planned, *a, **kw):
        evil = json.dumps({**prog, "resources": {**prog["resources"], "evil": {"type": POOL, "properties": {}}}})
        if what == "program":
            (wd / "Pulumi.yaml").write_text(evil)
        else:
            (wd / infra.PLAN_FILE).write_text(json.dumps({"fake": True, "steps": {"urn:x": ["delete"]}}))
        return real_apply(s, wd, stack, env, ev, planned, *a, **kw)
    monkeypatch.setattr(infra, "apply", apply)
    with pytest.raises(guard.GuardError, match="changed between the plan and the up|no longer is") as e:
        deploy(estate)
    assert ups(w) == [] and w.created == [] and "nothing was applied" in str(e.value)


def test_project_verify_is_called_before_the_up(estate, monkeypatch):
    """pipeline.run checks the directory itself too (render.Project.verify)."""
    w = estate.stack(fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}}))
    real_plan = infra.plan

    def plan(s, wd, *a, **kw):
        out = real_plan(s, wd, *a, **kw)
        (wd / "program").unlink()
        (wd / "program").write_text("{}")
        return out
    monkeypatch.setattr(infra, "plan", plan)
    with pytest.raises(guard.GuardError, match="program changed between the plan and the up.*planned: .*sha256"):
        deploy(estate)
    assert ups(w) == []


def test_two_renders_get_two_directories_and_the_settings_file_is_kept(estate):
    estate.stack(fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}}))
    s, ev, st = estate.s, Emitter(lambda e: None), estate.stacks["mini-guests"]
    a, b = render.Run(s, "deploy"), render.Run(s, "preview")
    pa, pb = render.render(s, "mini-guests", st, ev, a), render.render(s, "mini-guests", st, ev, b)
    assert pa.wd != pb.wd and pa.wd.parent == a.dir and (pa.sha256, pa.run_sha256) == (pb.sha256, pb.run_sha256)
    assert not (pa.wd / "Pulumi.main.yaml").exists()  # a new stack: Pulumi makes it
    # Pulumi makes the settings file (the passphrase salt) on the stack's first run: it is kept ...
    (pa.wd / "Pulumi.main.yaml").write_text("encryptionsalt: v1:first\n")
    render.settings_out(s, "mini-guests", pa.wd)
    kept = render.settings_file(s, "mini-guests")
    assert kept == s.state_dir / "stacks" / "mini-guests" / "Pulumi.main.yaml" and kept.read_text() == "encryptionsalt: v1:first\n"
    # ... never overwritten by a later run ...
    (pb.wd / "Pulumi.main.yaml").write_text("encryptionsalt: v1:second\n")
    render.settings_out(s, "mini-guests", pb.wd)
    assert kept.read_text() == "encryptionsalt: v1:first\n"
    # ... and copied into every run's directory.
    c = render.Run(s, "deploy")
    pc = render.render(s, "mini-guests", st, ev, c)
    assert (pc.wd / "Pulumi.main.yaml").read_text() == "encryptionsalt: v1:first\n"
    for r in (a, b, c):
        r.close()
    assert estate.runs() == [] and kept.exists()


def test_settings_file_of_the_old_work_dir_is_taken_over(estate):
    estate.stack(fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}}))
    s = estate.s
    legacy = s.state_dir / "work" / "mini-guests"
    legacy.mkdir(parents=True)
    (legacy / "Pulumi.main.yaml").write_text("encryptionsalt: v1:legacy\n")
    with render.Run(s, "deploy") as r:
        p = render.render(s, "mini-guests", estate.stacks["mini-guests"], Emitter(lambda e: None), r)
        assert (p.wd / "Pulumi.main.yaml").read_text() == "encryptionsalt: v1:legacy\n"
    assert render.settings_file(s, "mini-guests").read_text() == "encryptionsalt: v1:legacy\n"


def test_stale_run_directories_are_swept(estate):
    import os
    s = estate.s
    root = s.state_dir / "runs"
    root.mkdir(parents=True)
    dead_old, dead_new, alive_old = root / "20200101T000000-999999999-aa", root / "20200101T000000-999999998-bb", \
        root / f"20200101T000000-{os.getppid()}-cc"
    for d in (dead_old, dead_new, alive_old):
        d.mkdir()
    old = time.time() - render.STALE - 60
    os.utime(dead_old, (old, old))
    os.utime(alive_old, (old, old))
    with render.Run(s, "deploy"):
        assert not dead_old.exists() and dead_new.exists() and alive_old.exists()


def test_render_command_writes_to_its_own_directory(estate, monkeypatch):
    estate.stack(fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}}))
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    base = ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir), "render", "mini"]
    a, b = (CliRunner().invoke(cli, base).output.split()[0] for _ in range(2))
    assert a != b and Path(a).is_dir() and Path(b).is_dir() and "/runs/" in a
    assert json.loads((Path(a) / "Pulumi.yaml").read_text())["name"] == "mini-guests"


# ── F5: names are scoped to a stack ────────────────────────────────────────
@pytest.fixture
def twins(estate):
    """Two stacks, each with a guest called web."""
    a, b = guests(), {**guests(), "name": "mini-more"}
    wa, wb = estate.stack(a), estate.stack(b)
    for w, prog in ((wa, a), (wb, b)):
        for k in prog["resources"]:
            w.in_state(prog, k)
    return estate, wa, wb, a, b


def test_a_bare_name_that_two_stacks_have_is_refused_as_ambiguous(twins):
    estate, wa, wb, a, b = twins
    wb.force["web"] = {"steps": REPLACE}
    for preview in (True, False):
        with pytest.raises(guard.GuardError) as e:
            deploy(estate, allow_replace=["web"], preview=preview)
        assert "--allow-replace web is ambiguous" in str(e.value)
        assert "--allow-replace mini-guests/web or --allow-replace mini-more/web" in str(e.value)
    assert wa.calls == [] and wb.calls == []  # before any engine ran
    # A key only one stack has stays usable bare; an unknown stack is said.
    with pytest.raises(guard.GuardError, match="no stack nope in this request"):
        deploy(estate, allow_replace=["nope/web"])


def test_a_stack_scoped_name_allows_that_stack_only(twins):
    estate, wa, wb, a, b = twins
    wa.force["web"] = {"steps": REPLACE}
    wb.force["web"] = {"steps": REPLACE}
    # Named in mini-more only: mini-guests' web is still refused, and the flag says which.
    with pytest.raises(guard.GuardError) as e:
        deploy(estate, allow_replace=["mini-more/web"])
    assert "mini-guests: replace of guest web" in str(e.value) and "mini-more:" not in str(e.value)
    assert "needs --allow-replace mini-guests/web" in str(e.value)
    assert ups(wa) == [] and ups(wb) == []
    # The unnamed web is protected in its run copy; the named one is not.
    assert wa.calls[-1]["protect"] == ["db", "web"] and wb.calls[-1]["protect"] == ["db"]
    out = deploy(estate, allow_replace=["mini-more/web", "mini-guests/web"])
    assert wa.destroyed == ["web"] and wb.destroyed == ["web"] and out["applied"] == ["mini-guests", "mini-more"]


def test_a_urn_names_one_resource_and_a_single_stack_request_takes_bare_keys(twins):
    estate, wa, wb, a, b = twins
    wb.force["web"] = {"steps": REPLACE}
    deploy(estate, allow_replace=[fakes.urn(b, "web")])
    assert wb.destroyed == ["web"] and wa.destroyed == []
    wb.destroyed.clear()
    deploy(estate, stacks=["mini-more"], allow_replace=["web"])  # one stack in the request: not ambiguous
    assert wb.destroyed == ["web"]


def test_a_name_that_is_in_one_program_and_in_another_stacks_state_is_ambiguous(twins):
    """web was removed from mini-guests' program but is still in its state: a
    bare --allow-delete web must not also mean mini-more's web."""
    estate, wa, wb, a, b = twins
    del a["resources"]["web"]
    estate.stack(a)
    with pytest.raises(guard.GuardError, match="--allow-delete web is ambiguous"):
        deploy(estate, allow_delete=["web"])
    deploy(estate, allow_delete=["mini-guests/web"])
    assert wa.destroyed == ["web"] and wb.destroyed == []


def test_scope_and_protect_helpers():
    allow = guard.Allow(replace=["a/web", "b/web", "db", "urn:pulumi:main::p::t:i/x:X::k"], delete=["a/old"])
    assert guard.scope(allow, "a").replace == ["web", "db", "urn:pulumi:main::p::t:i/x:X::k"]
    assert guard.scope(allow, "b").delete == [] and guard.scope(allow, "a").delete == ["old"]
    prog = guests()
    out, added = guard.protect("main", prog, guard.Allow(replace=["web"], update=["db"]))
    assert added == ["db"] and out["resources"]["db"]["options"]["protect"] is True
    assert "protect" not in out["resources"]["web"]["options"] and "protect" not in out["resources"]["apps"]["options"]
    assert "protect" not in json.dumps(prog)  # a copy
    out, added = guard.protect("main", prog, guard.Allow(delete=[fakes.urn(prog, "db")]))
    assert added == ["web"]
