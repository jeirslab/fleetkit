"""The guard every `up` passes (guard.py, pipeline.py), with the fake engine:
a plan that replaces, deletes or updates a guest is refused unless the guest
is named for that op; other resources are listed and let through; a program
that carries `import` is refused; summaries name resources. Offline."""
import json
import time

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import fakes
from fakes import CT, POOL, VM
from fleetkit_cli import guard, pipeline, render
from fleetkit_cli.api import create_app
from fleetkit_cli.events import Emitter
from fleetkit_cli.gitops import _comment
from fleetkit_cli.jobs import JobManager
from fleetkit_cli.main import cli
from fleetkit_cli.pipeline import DeployRequest

REPLACE = ["create-replacement", "replace", "delete-replaced"]


def guests(**over):
    return fakes.program(
        web={"type": CT, "properties": {"nodeName": "pve1", "vmId": 101, "cores": 2}, "options": {"protect": True}},
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


def test_guest_types_are_the_proxmox_container_and_vm():
    assert guard.GUEST_TYPES == {CT, VM}


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
    assert len(ups(w)) == 1 and out["refused"] == {}
    assert [c["key"] for c in out["plan"]["mini-guests"]] == [key]


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


def test_up_is_cancelled_when_it_starts_a_step_the_plan_did_not_have(settled):
    estate, w, _ = settled
    w.drift["web"] = {"steps": REPLACE}
    with pytest.raises(guard.GuardError, match="cancelled.*create-replacement of guest web"):
        deploy(estate)
    assert w.cancelled == 1 and w.destroyed == []


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
