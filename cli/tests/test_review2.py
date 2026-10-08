"""The findings of the second review, with the fake engine (fakes.py) and
without any engine: pending operations, protect in state before the plan and
after the up, a guest held by two state entries, HA resources and unknown
proxmox types, what `absent` means in an adoption, adoption ids that name one
resource twice or one another stack holds, the temporary program leaving the
state's `protect` alone, the lock of a run directory, and the kill of a
session. The same against the real engine: test_real_review2.py. Offline."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

import fakes
from fakes import CT, POOL, VM
from fleetkit_cli import adopt, guard, guests, infra, pipeline, render
from fleetkit_cli.events import Emitter
from fleetkit_cli.main import cli
from fleetkit_cli.pipeline import DeployRequest

REPLACE = ["create-replacement", "replace", "delete-replaced"]
HA = "proxmox:index/haresource:Haresource"
WEB = {"nodeName": "pve1", "vmId": 101, "cores": 2}
DB = {"nodeName": "pve1", "vmId": 102}


def guests_prog(**over):
    return fakes.program(
        web={"type": CT, "properties": dict(WEB)},
        db={"type": VM, "properties": dict(DB)},
        apps={"type": POOL, "properties": {"poolId": "apps"}}, **over)


def deploy(estate, events=None, **req):
    ev = Emitter((events if events is not None else []).append)
    return pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False, **req), ev)


def ups(world):
    return [c for c in world.calls if c["verb"] == "up"]


@pytest.fixture
def settled(estate):
    """Everything of the program in state, as a runner without the state pass
    left it: nothing protected."""
    prog = guests_prog()
    w = estate.stack(prog)
    for k in prog["resources"]:
        w.in_state(prog, k)
    return estate, w, prog


# ── F2: pending operations ─────────────────────────────────────────────────
@pytest.mark.parametrize("preview", [True, False])
def test_a_pending_operation_refuses_the_preview_and_the_deploy(settled, preview):
    """An interrupted create leaves `creating <urn>` in the state. Pulumi warns
    and creates it again: the second guest. Nothing runs while one is there."""
    estate, w, prog = settled
    prog["resources"]["vm9"] = {"type": VM, "properties": {"nodeName": "pve1", "vmId": 109},
                                "options": {"provider": "${provider-proxmox}"}}
    estate.stack(prog)
    urn = fakes.urn(prog, "vm9")
    w.pending = [{"type": "creating", "resource": {"urn": urn, "type": VM}}]
    with pytest.raises(guard.GuardError) as e:
        deploy(estate, preview=preview)
    text = str(e.value)
    assert "1 pending operation(s)" in text and f"creating vm9 ({urn})" in text
    # What the operator must check, and both ways out.
    assert "does the resource exist" in text and "fleetkit adopt" in text and "pending_operations" in text
    assert "second guest" in text and "pulumi stack import" in text
    assert e.value.result["pending"] == {"mini-guests": [{"type": "creating", "urn": urn, "key": "vm9"}]}
    # No engine ran, nothing was created, and the state was not written (no protect either).
    assert w.calls == [] and w.created == [] and w.protected == []
    # Without it: the same request goes through.
    w.pending = []
    out = deploy(estate, preview=preview)
    assert [c["key"] for c in out["plan"]["mini-guests"]] == ["vm9"]


def test_a_run_stopped_with_a_step_in_flight_says_what_to_check(estate):
    """The cancel message is the first place the operator reads about it."""
    w = estate.stack(guests_prog())
    ev = Emitter(lambda e: None)

    def on_step(verb, op, key):
        if verb == "up" and key == "web":
            ev.cancel()
            ev.cancel()
    w.on_step = on_step
    with pytest.raises(pipeline.Cancelled) as e:
        pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False), ev)
    text = str(e.value)
    assert "In flight when it stopped: create web; whether they happened is not known" in text
    assert "pending operation for create web" in text and "is refused until it is settled" in text
    assert "does the resource exist" in text and "adopt it first" in text and "pending_operations" in text


def test_a_run_that_stopped_between_steps_does_not_talk_of_pending_operations(estate):
    w = estate.stack(guests_prog())
    ev = Emitter(lambda e: None)
    # One cancel: the step in flight finishes, and the engine saves the state.
    w.on_step = lambda verb, op, key: ev.cancel() if verb == "up" and key == "web" else None
    with pytest.raises(pipeline.Cancelled) as e:
        pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False), ev)
    assert "No step was in flight" in str(e.value) and "pending operation" not in str(e.value)


# ── F3: protect in state before the first plan ─────────────────────────────
def test_the_state_protects_unnamed_guests_before_the_first_plan(settled):
    """`protect` in the program alone does not stop the replace of a guest the
    state does not protect yet (seen with a real engine; the fake does the
    same). First contact with a state nothing protected: the engine refuses."""
    estate, w, prog = settled
    seen = []
    w.on_step = lambda verb, op, key: seen.append((verb, list(w.protected))) if not seen else None
    w.ignore_plan = True                     # the plan layer out of the way
    w.drift["web"] = {"steps": REPLACE}      # and a replace the preview did not have
    events = []
    with pytest.raises(infra.EngineError, match="marked for protection"):
        deploy(estate, events)
    web, db = fakes.urn(prog, "web"), fakes.urn(prog, "db")
    assert w.destroyed == [] and w.stops == []          # the engine itself refused; no tripwire was needed
    assert seen == [("preview", [web, db])]             # protected before the first step of the first preview
    assert sorted(e["urn"] for e in events if e["kind"] == "protect") == sorted([web, db])
    assert fakes.urn(prog, "apps") not in w.protected   # a pool is no guest


def test_a_preview_writes_nothing_to_the_state(settled):
    estate, w, prog = settled
    deploy(estate, preview=True)
    assert w.protected == [] and not w.state[fakes.urn(prog, "web")].get("protect")


def test_a_guest_named_for_a_replace_or_a_delete_is_not_protected_before_the_plan(settled):
    estate, w, prog = settled
    w.force["web"] = {"steps": REPLACE}
    w.on_step = lambda verb, op, key: w.__dict__.setdefault("at_first_step", list(w.protected))
    deploy(estate, allow_replace=["web"], allow_delete=["db"])
    assert w.at_first_step == [] and w.destroyed == ["web"]
    # ... and afterwards every guest still in state is protected, the replaced one too.
    assert all(w.state[fakes.urn(prog, k)]["protect"] is True for k in ("web", "db"))


def test_a_guest_the_estate_protects_is_protected_in_state_although_it_is_named(settled):
    """The estate's own `protect` is never removed, and the engine only honours
    it once the state has it: the state pass sets it whatever is named."""
    estate, w, prog = settled
    prog["resources"]["web"]["options"]["protect"] = True
    estate.stack(prog)
    w.force["web"] = {"steps": REPLACE}
    with pytest.raises(infra.EngineError, match="marked for protection"):
        deploy(estate, allow_replace=["web"])
    assert w.protected == [fakes.urn(prog, "web"), fakes.urn(prog, "db")] and w.destroyed == []


def test_a_state_that_cannot_be_protected_refuses_the_deploy(settled):
    estate, w, prog = settled
    w.protect_fails = {fakes.urn(prog, "web")}
    with pytest.raises(infra.EngineError, match="nothing was applied: could not protect in state.*::web"):
        deploy(estate)
    assert w.calls == []


# ── F6: what a named delete leaves when its up does not happen ─────────────
def _named_delete(settled):
    estate, w, prog = settled
    deploy(estate)  # the state protects web and db
    web = fakes.urn(prog, "web")
    del prog["resources"]["web"]
    estate.stack(prog)
    w.calls.clear()
    w.protected.clear()
    return estate, w, prog, web


def test_a_named_delete_whose_up_fails_leaves_the_guest_protected(settled):
    """The runner unprotects a guest named for a delete in state, where no
    program can. When the up then fails before the delete, the guest stayed in
    state unprotected, for whatever came next."""
    estate, w, prog, web = _named_delete(settled)

    def on_step(verb, op, key):
        if verb == "up":
            raise RuntimeError("the provider fell over")
    w.on_step = on_step
    events = []
    with pytest.raises(infra.EngineError, match="the provider fell over"):
        deploy(estate, events, allow_delete=["web"])
    assert w.unprotected == [web] and w.destroyed == [] and web in w.state
    assert w.state[web]["protect"] is True and w.protected == [web]
    kinds = [e["kind"] for e in events if e["kind"] in ("unprotect", "protect", "up")]
    assert kinds == ["unprotect", "up", "protect"]


def test_a_named_delete_that_is_cancelled_leaves_the_guest_protected(settled):
    estate, w, prog, web = _named_delete(settled)
    ev = Emitter(lambda e: None)

    def on_step(verb, op, key):
        if verb == "up":
            ev.cancel()
            ev.cancel()  # at once: the delete never starts
    w.on_step = on_step
    with pytest.raises(pipeline.Cancelled):
        pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False, allow_delete=["web"]), ev)
    assert w.unprotected == [web] and w.destroyed == [] and w.state[web]["protect"] is True


def test_a_second_plan_that_differs_after_unprotecting_leaves_the_guest_protected(settled, monkeypatch):
    estate, w, prog, web = _named_delete(settled)
    first = []

    def on_step(verb, op, key):  # the plan after the unprotect has something the first had not
        if w.unprotected and "db" not in w.force:
            w.force["db"] = {"steps": ["update"]}
    w.on_step = on_step
    with pytest.raises(guard.GuardError, match="not the one that was checked.*protected in state again"):
        deploy(estate, allow_delete=["web"])
    assert not first and w.destroyed == [] and w.state[web]["protect"] is True


def test_a_guest_that_cannot_be_protected_again_is_said_loudly(settled):
    estate, w, prog, web = _named_delete(settled)

    def on_step(verb, op, key):
        if verb == "up":
            w.protect_fails = {web}
            raise RuntimeError("the provider fell over")
    w.on_step = on_step
    events = []
    with pytest.raises(infra.EngineError) as e:
        deploy(estate, events, allow_delete=["web"])
    assert "the provider fell over" in str(e.value)
    assert "GUESTS ARE LEFT UNPROTECTED IN STATE" in str(e.value) and web in str(e.value)
    assert "pulumi state protect" in str(e.value)
    (err,) = [x for x in events if x["kind"] == "error"]
    assert err["unprotected"] == [web] and "LEFT UNPROTECTED" in err["text"]


def test_a_deploy_that_succeeds_and_cannot_protect_says_so_in_its_result(settled):
    estate, w, prog = settled
    w.force["web"] = {"steps": REPLACE}
    w.on_step = lambda verb, op, key: w.protect_fails.add(fakes.urn(prog, "web")) if verb == "up" else None
    out = deploy(estate, allow_replace=["web"])
    assert out["applied"] == ["mini-guests"] and "LEFT UNPROTECTED" in out["unprotected"]["mini-guests"]


# ── F4 (d): a guest two state entries hold ─────────────────────────────────
def test_the_delete_of_a_guest_another_state_entry_holds_is_refused_whatever_is_named(settled):
    """The state has web twice (an adoption under a second key): dropping one
    key plans a delete of that entry, which destroys the guest the other entry
    still manages. The refusal used to print `--allow-delete web0`."""
    estate, w, prog = settled
    w.state[fakes.urn(prog, "web")]["id"] = "101"
    twin = fakes.urn(prog, "web").replace("::web", "::web0")
    w.state[twin] = {"type": CT, "id": "101", "importID": "pve1/101", "inputs": dict(WEB)}
    for kw in ({}, {"allow_delete": ["web0"]}, {"allow_delete": [twin]}):
        with pytest.raises(guard.GuardError) as e:
            deploy(estate, **kw)
        text = str(e.value)
        assert "delete of guest web0" in text and "is also in the state of mini-guests as" in text
        assert fakes.urn(prog, "web") in text and "--allow-delete" not in text
        assert "pulumi state delete" in text and "destroys the guest the other still manages" in text
        (r,) = e.value.result["refused"]["mini-guests"]
        assert r["flag"] is None and r["op"] == "delete" and r["key"] == "web0"
    assert ups(w) == [] and w.destroyed == [] and w.unprotected == []
    out = deploy(estate, preview=True, allow_delete=["web0"])
    assert out["refused"]["mini-guests"][0]["flag"] is None
    # Another vmid, or the same id on something that is no guest: an ordinary delete.
    w.state[twin]["id"], w.state[twin]["importID"] = "1101", "pve1/1101"
    with pytest.raises(guard.GuardError, match="needs --allow-delete web0"):
        deploy(estate)


def test_a_guest_held_by_another_stack_of_the_run_is_not_deleted(estate):
    """The guest moved from one stack to another and was adopted there: the
    stack that dropped it plans its delete."""
    a, b = guests_prog(), {**guests_prog(), "name": "mini-more"}
    wa, wb = estate.stack(a), estate.stack(b)
    for w, prog in ((wa, a), (wb, b)):
        for k in prog["resources"]:
            w.in_state(prog, k, f"{prog['name']}:{k}")
    # One vmid, under two type tokens of the VM family, in two stacks.
    wa.state[fakes.urn(a, "db")]["id"] = "102"
    wb.state[fakes.urn(b, "db")].update(id="102", importID="pve1/102", type="proxmox:index/vm:Vm")
    del a["resources"]["db"]
    estate.stack(a)
    with pytest.raises(guard.GuardError) as e:
        deploy(estate, allow_delete=["mini-guests/db"])
    assert "is also in the state of mini-more as" in str(e.value) and "--allow-delete" not in str(e.value)
    assert wa.destroyed == [] and wb.destroyed == [] and ups(wa) == [] and ups(wb) == []


def test_ident_and_same_resource():
    vm2 = "proxmox:index/virtualEnvironmentVm2:VirtualEnvironmentVm2"
    assert guard.ident(VM, "pve1/102") == guard.ident(vm2, "102") == guard.ident("proxmox:index/clonedVm:ClonedVm", 102)
    assert guard.ident(CT, "pve1/102") != guard.ident(VM, "pve1/102")       # a container is no VM
    assert guard.ident(POOL, "pve1/102") == (POOL, "pve1/102")              # no guest: the id as it is
    assert guard.same_resource(VM, "pve2/102", {"type": vm2, "id": "102"})
    assert guard.same_resource(VM, "102", {"type": "proxmox:index/vm:Vm", "id": "9", "importID": "pve1/102"})
    assert not guard.same_resource(VM, "pve1/102", {"type": CT, "id": "102"})
    assert not guard.same_resource(VM, "pve1/102", {"type": VM, "id": "1102"})
    assert guests.VM_TYPES | guests.CONTAINER_TYPES == guests.GUEST_TYPES and len(guests.VM_TYPES) == 5


# ── F9: HA resources, and types nobody decided ─────────────────────────────
def ha(state="started"):
    return {"type": HA, "properties": {"resourceId": "vm:102", "state": state}}


def test_an_ha_resource_is_gated_like_a_guest_and_its_create_too(settled):
    """Creating, changing or deleting an HA resource makes the HA manager
    start, stop or move the guest it names."""
    estate, w, prog = settled
    estate.stack(guests_prog(ha=ha("stopped")))
    with pytest.raises(guard.GuardError) as e:
        deploy(estate)
    assert "create of HA resource ha" in str(e.value) and "needs --allow-update ha" in str(e.value)
    (r,) = e.value.result["refused"]["mini-guests"]
    assert (r["op"], r["flag"], r["steps"]) == ("create", "--allow-update ha", ["create"])
    assert ups(w) == [] and w.created == []
    for wrong in ({"allow_replace": ["ha"]}, {"allow_delete": ["ha"]}, {"allow_update": ["db"]}):
        with pytest.raises(guard.GuardError):
            deploy(estate, **wrong)
    deploy(estate, allow_update=["ha"])
    assert w.created == ["ha"]
    # Protected for the run like a guest, in the program and then in state.
    assert "ha" in w.calls[-1]["protect"]
    # An update, and a delete.
    estate.stack(guests_prog(ha=ha("started")))
    with pytest.raises(guard.GuardError, match="update of HA resource ha .*needs --allow-update ha"):
        deploy(estate)
    estate.stack(guests_prog())
    with pytest.raises(guard.GuardError, match="delete of HA resource ha .*needs --allow-delete ha"):
        deploy(estate)
    assert w.updated == [] and w.destroyed == []
    deploy(estate, allow_delete=["ha"])
    assert w.destroyed == ["ha"]


def test_the_create_of_a_guest_is_still_not_gated(estate):
    w = estate.stack(guests_prog())
    deploy(estate)
    assert sorted(w.created) == ["apps", "db", "provider-proxmox", "web"]


@pytest.mark.parametrize("type_", ["proxmox:index/lxcNext:LxcNext", "proxmoxve:vm/virtualMachine:VirtualMachine"])
def test_a_proxmox_type_the_runner_does_not_know_is_refused(settled, type_):
    """A provider version with a type the lists do not have (a new kind of
    guest, say): nothing is applied while the program has one, whatever is
    named."""
    estate, w, prog = settled
    estate.stack(guests_prog(new={"type": type_, "properties": {"vmId": 300}}))
    for kw in ({}, {"allow_replace": ["new"], "allow_delete": ["new"], "allow_update": ["new"]}):
        with pytest.raises(guard.GuardError) as e:
            deploy(estate, **kw)
        assert f"new ({type_})" in str(e.value) and "in none of the runner's lists" in str(e.value)
        (r,) = e.value.result["refused"]["mini-guests"]
        assert (r["op"], r["flag"], r["key"]) == ("unknown-type", None, "new")
    assert ups(w) == [] and w.created == []
    out = deploy(estate, preview=True)
    assert out["refused"]["mini-guests"][0]["op"] == "unknown-type"
    # One that is only in state any more (the program dropped it): its delete is refused too.
    w.state[fakes.urn(guests_prog(new={"type": type_}), "new")] = {"type": type_, "id": "300", "inputs": {}}
    estate.stack(guests_prog())
    with pytest.raises(guard.GuardError, match="in none of the runner's lists"):
        deploy(estate, allow_delete=["new"])
    assert w.destroyed == []


def test_known_and_unknown_types():
    assert not guard.unknown(POOL) and not guard.unknown(CT) and not guard.unknown(HA)
    assert not guard.unknown("pulumi:providers:proxmox") and not guard.unknown("github:index/repository:Repository")
    assert guard.unknown("proxmox:index/vm9:Vm9") and guard.unknown("proxmoxve:index/thing:Thing")
    assert guard.gated(HA) and guard.gated(CT) and not guard.gated(POOL)
    assert guard.gate(HA, "create") == "update" and guard.gate(CT, "create") is None
    assert guard.gate(HA, "delete") == "delete" and guard.gate(CT, "delete-replaced") == "replace"
    assert not guests.HA_TYPES & guests.GUEST_TYPES and guests.GATED_TYPES == guests.GUEST_TYPES | guests.HA_TYPES


# ── F8: the lock of a run directory ────────────────────────────────────────
def _aged(d):
    old = time.time() - render.STALE - 60
    os.utime(d, (old, old))


def test_a_live_run_is_not_swept_whatever_its_pid_says(estate):
    """The pid in the name cannot tell two runs of one `serve` process apart,
    nor see a run on another host. A run holds a lock; what is swept is what
    can be locked."""
    s = estate.s
    mine = render.Run(s, "deploy")            # a long run of this very process
    (mine.dir / "x").mkdir()
    _aged(mine.dir)
    root = mine.dir.parent
    far = root / "20200101T000000-999999999-aa"   # a pid that does not exist here: another host's run
    far.mkdir()
    held = os.open(far / render.LOCK, os.O_CREAT | os.O_RDWR)
    import fcntl
    fcntl.flock(held, fcntl.LOCK_EX)
    _aged(far)
    over = root / "20200101T000000-999999998-bb"  # the same, its run over: the lock is free
    over.mkdir()
    (over / render.LOCK).touch()
    _aged(over)
    try:
        with render.Run(s, "deploy"):
            assert mine.dir.exists() and far.exists() and not over.exists()
        assert render.sweep(root) == []
    finally:
        os.close(held)
    # Released (the process died, or the run closed): swept.
    assert render.sweep(root) == [far.name] and mine.dir.exists()
    mine.close()
    assert not mine.dir.exists()


def test_a_run_holds_its_lock_until_it_closes(estate):
    import fcntl
    run = render.Run(estate.s, "deploy")
    fd = os.open(run.dir / render.LOCK, os.O_RDWR)
    try:
        with pytest.raises(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run.close()
        assert not run.dir.exists()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the unlinked file: free now
    finally:
        os.close(fd)
    kept = render.Run(estate.s, "render", keep=True)
    kept.close()
    assert kept.dir.exists() and render.sweep(kept.dir.parent) == []  # not a day old
    _aged(kept.dir)
    assert render.sweep(kept.dir.parent) == [kept.dir.name]


# ── F1: the kill reaches every process of the session ──────────────────────
LEADER = """
import os, subprocess, sys, time
# As Pulumi starts a plugin: a child in a process group of its own.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], process_group=0)
print(child.pid, flush=True)
time.sleep(300)
"""


def _alive(pid):
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="no /proc: sessions cannot be listed here")
def test_the_kill_takes_the_whole_session_not_the_process_group():
    """Pulumi gives each plugin its own process group inside the engine's
    session: `killpg` of the engine left the provider plugin running, and with
    it the call in flight."""
    leader = subprocess.Popen([sys.executable, "-c", LEADER], stdout=subprocess.PIPE, text=True,
                              start_new_session=True)
    try:
        plugin = int(leader.stdout.readline())
        assert os.getsid(plugin) == leader.pid and os.getpgid(plugin) != os.getpgid(leader.pid)
        assert infra.session_processes(leader.pid).keys() == {leader.pid, plugin}
        cmd = infra.OwnedPulumi.__new__(infra.OwnedPulumi)
        cmd.signals = []
        count, left = cmd._kill([leader])
        leader.wait(5)
        assert (count, left, cmd.signals) == (2, [], ["SIGKILL"])
        for _ in range(100):
            if not _alive(plugin):
                break
            time.sleep(0.02)
        assert not _alive(plugin) and infra.session_processes(leader.pid) == {}
    finally:
        for pid in (leader.pid, locals().get("plugin")):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, TypeError):
                pass


def test_what_survives_a_kill_is_reported(monkeypatch):
    monkeypatch.setattr(infra, "kill_session", lambda sid, wait=None: ({sid}, {4242: "pulumi-resource-proxmox"}))
    cmd = infra.OwnedPulumi.__new__(infra.OwnedPulumi)
    cmd.signals = []
    count, left = cmd._kill([subprocess.Popen(["true"])])
    assert count == 1 and left == ["pid 4242 (pulumi-resource-proxmox)"]
    said = infra.OwnedPulumi._survivors(left)
    assert "STILL RUNNING after the kill: pid 4242 (pulumi-resource-proxmox)" in said and "kill it by hand" in said
    assert infra.OwnedPulumi._survivors([]) == ""


# ── adopt ──────────────────────────────────────────────────────────────────
def adopt_prog():
    return fakes.program(
        web={"type": CT, "properties": dict(WEB)},
        db={"type": VM, "properties": dict(DB)},
        apps={"type": POOL, "properties": {"poolId": "apps"}})


@pytest.fixture
def lab(estate):
    """web and db exist for real and are not in state; apps is in state."""
    p = adopt_prog()
    w = estate.stack(p, adopt_ids={"web": "pve1/101", "db": "pve1/102", "apps": "apps"})
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


# ── F5: what `absent` means ────────────────────────────────────────────────
@pytest.mark.parametrize("message,status", [
    ("Preview failed: resource 'pve2/105' does not exist", "absent"),
    ("resource 'pve2/105' does not exist", "absent"),
    # The guest lives on another node: bpg's error for the node that was asked.
    ("error retrieving container: received an HTTP 500 response - Reason: Configuration file "
     "'nodes/pve2/lxc/105.conf' does not exist", "error"),
    ('error reading container: Get "https://pve:8006/api2/json/nodes/pve/lxc/105/config": proxy error: '
     "404 page not found", "error"),
    ("provider binary terraform-provider-proxmox not found in cache", "error"),
    ("received an HTTP 401 response - Reason: authentication failure", "error"),
    ("Preview failed: resource 'pve2/1050' does not exist", "error"),      # another id
    ("warning: resource 'pve2/105' does not exist yet, retrying", "error"),  # not the whole message
])
def test_absent_is_only_the_engines_exact_words(message, status):
    program = fakes.program(ct={"type": CT, "properties": {}})
    urn = guard.urn_of("main", program, "ct")
    ev = Emitter(lambda e: None)
    for extra in ([], ["import failed"]):
        p = guard.Plan("main", program)
        p.error(urn, message)
        for m in extra:
            p.error(urn, m)
        r = adopt._resource(ev, p, program, "ct", urn, "pve2/105", None)
        assert r["status"] == status, r
    # Its own words beside another error: an error.
    p = guard.Plan("main", program)
    p.error(urn, "Preview failed: resource 'pve2/105' does not exist")
    p.error(urn, "received an HTTP 500 response")
    assert adopt._resource(ev, p, program, "ct", urn, "pve2/105", None)["status"] == "error"


def test_a_provider_error_that_mentions_not_found_is_an_error(lab):
    estate, w, p = lab
    w.import_error[(CT, "pve1/101")] = ("error retrieving container: received an HTTP 500 response - Reason: "
                                        "Configuration file 'nodes/pve1/lxc/101.conf' does not exist")
    report = run(estate, apply=True)
    assert statuses(report)["web"] == "error" and not report["ok"] and not report["applied"]
    assert "101.conf" in entry(report, "web")["error"] and fakes.urn(p, "db") not in w.state


def test_an_absent_guest_refuses_the_apply_unless_accepted(lab, monkeypatch):
    """Nothing by this id: a deploy would create the guest. If it does exist,
    under another id, that is a second one; so an apply wants it said."""
    estate, w, p = lab
    del w.live[(CT, "pve1/101")]
    report = run(estate)  # the report alone: an absent guest is no failure
    assert statuses(report) == {"web": "absent", "db": "import", "apps": "in-state"} and report["ok"]
    (r,) = report["refused"]
    assert (r["key"], r["kind"]) == ("web", "absent") and "--accept-absent web" in r["why"]
    assert "CREATE this guest" in r["why"] and "--id web=<node>/<vmid>" in r["why"]
    text = "\n".join(adopt.text(report))
    assert "web: absent: pve1/101" in text and "an apply would refuse: mini-guests web" in text
    report = run(estate, apply=True)
    assert not report["ok"] and not report["applied"] and fakes.urn(p, "db") not in w.state
    assert all(c["verb"] == "preview" for c in w.calls)
    # Accepted by key: the rest is adopted, the absent one is left for a deploy.
    with pytest.raises(adopt.AdoptError, match="--accept-absent: no resource nope"):
        run(estate, apply=True, accept_absent=["nope"])
    report = run(estate, apply=True, accept_absent=["db"])  # another key accepts nothing
    assert not report["applied"]
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    r = CliRunner().invoke(cli, ["--flake", str(estate.repo), "--state-dir", str(estate.s.state_dir), "adopt",
                                 "mini", "--apply", "--accept-absent", "web", "--json"])
    doc = json.loads(r.stdout)
    assert r.exit_code == 0 and doc["applied"] and doc["ok"] and doc["refused"] == []
    assert fakes.urn(p, "db") in w.state and fakes.urn(p, "web") not in w.state


def test_an_absent_resource_that_is_no_guest_needs_no_accepting(lab):
    estate, w, p = lab
    p["resources"]["net"] = {"type": POOL, "properties": {"poolId": "net"},
                             "options": {"provider": "${provider-proxmox}"}}
    estate.stack(p, adopt_ids={"web": "pve1/101", "db": "pve1/102", "net": "net"})
    report = run(estate, apply=True)
    assert statuses(report)["net"] == "absent" and report["ok"] and report["applied"] and report["refused"] == []


# ── F4: one real resource, twice ───────────────────────────────────────────
def test_two_resources_of_a_run_with_one_id_are_both_refused(lab):
    """Two keys carry the adoption id of one guest. Both were imported: two
    state entries for it, and the delete of either destroys it."""
    estate, w, p = lab
    p["resources"]["web2"] = {"type": CT, "properties": dict(WEB), "options": {"provider": "${provider-proxmox}"}}
    estate.stack(p, adopt_ids={"web": "pve1/101", "web2": "pve2/101", "db": "pve1/102"})  # one vmid, however written
    report = run(estate, apply=True)
    assert statuses(report) == {"web": "duplicate", "web2": "duplicate", "db": "import"}
    assert entry(report, "web")["same"] == ["web2"] and "web and web2 resolve to the same real resource" in \
        entry(report, "web")["why"]
    assert not report["ok"] and not report["applied"]
    assert sorted(r["key"] for r in report["refused"] if r["kind"] == "duplicate") == ["web", "web2"]
    # Neither was ever given to the engine with an import, and nothing was imported.
    assert all(set(c["imports"]) <= {"db"} for c in w.calls) and ups(w) == []
    assert not [u for u in w.state if u.endswith(("::web", "::web2", "::db"))]
    # Without --apply it is a failure all the same.
    assert not run(estate)["ok"]


def test_one_id_in_two_stacks_of_a_run_is_refused_in_both(lab):
    estate, w, p = lab
    more = {**fakes.program(vm={"type": "proxmox:index/vm:Vm", "properties": dict(DB)}), "name": "mini-more"}
    w2 = estate.stack(more, adopt_ids={"vm": "pve1/102"})  # mini-guests' db, under another VM token
    w2.live[("proxmox:index/vm:Vm", "pve1/102")] = dict(DB)
    report = run(estate, apply=True)
    assert statuses(report)["db"] == "duplicate" and statuses(report, "mini-more") == {"vm": "duplicate"}
    assert entry(report, "db")["same"] == ["mini-more/vm"] and not report["applied"] and not report["ok"]
    assert ups(w) == [] and ups(w2) == []


def test_an_id_another_stack_of_the_estate_holds_is_refused(lab):
    """The duplicate check looked at the adopting stack's state only. The
    guest is in the state of another stack, one the run did not even select."""
    estate, w, p = lab
    other = {**fakes.program(vm={"type": "proxmox:index/vm:Vm", "properties": dict(DB)}), "name": "mini-more"}
    w2 = estate.stack(other)
    held = fakes.urn(other, "vm")
    w2.state[held] = {"type": "proxmox:index/vm:Vm", "id": "102", "inputs": dict(DB)}
    for apply in (False, True):
        report = run(estate, apply=apply, stacks=["mini-guests"])
        db = entry(report, "db")
        assert db["status"] == "duplicate" and db["twin"] == held and db["twinStack"] == "mini-more"
        assert "already in the state of mini-more" in db["why"] and "pulumi state move" in db["why"]
        assert not report["ok"] and not report["applied"]
    assert all("db" not in c["imports"] for c in w.calls) and fakes.urn(p, "db") not in w.state and w2.calls == []
    # A stack of the estate whose state cannot be read: nothing is adopted.
    real = infra.state
    estate_stack = infra.open_stack

    def broken(s, wd, stack, *a, **kw):
        if stack == "mini-more":
            raise RuntimeError("backend unreachable")
        return estate_stack(s, wd, stack, *a, **kw)
    infra.open_stack = broken
    try:
        with pytest.raises(adopt.AdoptError, match="cannot read the state of mini-more.*backend unreachable"):
            run(estate, apply=True, stacks=["mini-guests"])
    finally:
        infra.open_stack = estate_stack
    assert infra.state is real and ups(w) == []


def test_a_guest_in_state_under_another_token_of_its_family_is_a_duplicate(lab):
    estate, w, p = lab
    old = "urn:pulumi:main::mini-guests::proxmox:index/virtualEnvironmentVm2:VirtualEnvironmentVm2::db-old"
    w.state[old] = {"type": "proxmox:index/virtualEnvironmentVm2:VirtualEnvironmentVm2", "id": "102", "inputs": {}}
    report = run(estate, apply=True)
    assert statuses(report)["db"] == "duplicate" and entry(report, "db")["twin"] == old and not report["applied"]
    # A container with that vmid is another thing (and cannot exist beside it).
    w.state[old]["type"] = CT
    assert statuses(run(estate))["db"] == "import"


def test_the_stacks_that_adopted_nothing_are_previewed_after_an_adoption(lab):
    """Another stack holds the adopted id in a form the check before the
    import does not match (here: another type), and its program dropped it:
    its next deploy deletes the live resource. Found after the import, by a
    preview of every stack of the estate that has state."""
    estate, w, p = lab
    other = {**fakes.program(), "name": "mini-more"}
    w2 = estate.stack(other)
    w2.in_state(other, "provider-proxmox")
    legacy = "urn:pulumi:main::mini-more::proxmox:index/virtualEnvironmentFile:VirtualEnvironmentFile::legacy"
    w2.state[legacy] = {"type": "proxmox:index/virtualEnvironmentFile:VirtualEnvironmentFile", "id": "pve1/102",
                        "inputs": {}}
    empty = {**fakes.program(), "name": "mini-empty"}
    w3 = estate.stack(empty)  # a stack without state: not previewed
    report = run(estate, apply=True, stacks=["mini-guests"])
    assert report["applied"] and not report["ok"]
    assert report["stacks"]["mini-guests"]["verify"]["ok"]
    v = report["stacks"]["mini-more"]["verify"]
    assert not v["ok"] and v["scope"] == "adopted ids" and [(c["key"], c["op"]) for c in v["destroys"]] == [
        ("legacy", "delete")]
    assert "WOULD DELETE legacy" in "\n".join(adopt.text(report))
    assert [c["verb"] for c in w2.calls] == ["preview"] and w3.calls == [] and "mini-empty" not in report["stacks"]
    # Without the stray entry: verified, and said so.
    del w2.state[legacy]
    for k in ("web", "db"):
        del w.state[fakes.urn(p, k)]
    report = run(estate, apply=True, stacks=["mini-guests"])
    assert report["ok"] and report["stacks"]["mini-more"]["verify"]["ok"]
    assert "deletes or replaces nothing that holds an adopted id" in "\n".join(adopt.text(report))


# ── F7: the temporary program and what is in state already ─────────────────
@pytest.mark.parametrize("protected", [False, True])
def test_an_adoption_leaves_the_protect_of_what_is_in_state_as_it_is(lab, protected):
    """db depends on web and on apps, which are in state: they are targets of
    the adoption's up. The temporary program used to carry `protect` on every
    guest (and on nothing else), and the up wrote that into their state."""
    estate, w, p = lab
    p["resources"]["db"]["options"]["dependsOn"] = ["${web}", "${apps}"]
    estate.stack(p, adopt_ids={"db": "pve1/102"})
    w.in_state(p, "web", "101", protect=protected)
    w.state[fakes.urn(p, "apps")]["protect"] = not protected  # a pool the state protects, or not
    def entries():  # the fake writes `protect: False` where the real state has no key
        return {u: {k: v for k, v in r.items() if k != "protect" or v} for u, r in w.state.items()}
    before = json.loads(json.dumps(entries()))
    report = run(estate, apply=True)
    assert report["applied"] and report["ok"], report
    up = next(c for c in w.calls if c["verb"] == "up")
    assert {fakes.urn(p, "web"), fakes.urn(p, "apps")} <= set(up["targets"])
    # In the temporary program: protect on what is adopted, and elsewhere what the state says.
    assert up["protect"] == sorted(["db", *(["web"] if protected else ["apps"])])
    for u, r in before.items():
        assert entries()[u] == r, u  # every entry that was in state: untouched
    assert w.state[fakes.urn(p, "db")]["protect"] is True and w.protected == [] and w.unprotected == []
