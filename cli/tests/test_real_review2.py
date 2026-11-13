"""The findings of the second review against the REAL Pulumi engine, offline
(as test_real_pulumi.py: a file backend in a temp dir, the `random` and `tls`
providers beside the binary, RandomString standing in for a guest): the kill
of an engine with a provider call in flight and the pending operation it
leaves, `protect` at first contact with a state that has none, a named delete
whose up fails, adoption ids that name one resource twice, and the `protect`
of what an adoption only depends on. test_review2.py has the same with the
fake engine, and what needs no engine."""
import os
import threading
import time

import pytest

from fleetkit_cli import adopt, guard, infra, pipeline, render
from fleetkit_cli.events import Cancelled, Emitter
from fleetkit_cli.pipeline import DeployRequest
from test_real_pulumi import KEY, RS, deploy, env, lab, locks, no_cancel, rs  # noqa: F401 - lab is a fixture

CERT = "tls:index/selfSignedCert:SelfSignedCert"


def prog(name="mini-guests", **res):
    return {"name": name, "runtime": "yaml", "resources": res}


def key(bits, **options):
    return {"type": KEY, "properties": {"algorithm": "RSA", "rsaBits": bits}, **({"options": options} if options else {})}


class Stack:
    """A stack of the estate, opened with the real CLI from a directory of its own."""

    def __init__(self, estate, name="mini-guests"):
        self.ev = Emitter(lambda e: None)
        self.run = render.Run(estate.s, "render")
        p = render.render(estate.s, name, estate.stacks[name], self.ev, self.run)
        self.st = infra.open_stack(estate.s, p.wd, name, env(estate), self.ev, install=False)

    def __enter__(self):
        return self.st

    def __exit__(self, *exc):
        self.run.close()


def exported(estate, name="mini-guests"):
    with Stack(estate, name) as st:
        return st.export_stack().deployment


def state(estate, name="mini-guests"):
    """urn name -> the state resource (not the stack, not the providers)."""
    return {x["urn"].rsplit("::", 1)[-1]: x for x in exported(estate, name).get("resources") or []
            if x["type"] != "pulumi:pulumi:Stack" and not x["type"].startswith("pulumi:providers:")}


def urn(key_, type_=RS, project="mini-guests"):
    return f"urn:pulumi:main::{project}::{type_}::{key_}"


def unprotect_in_state(estate, *keys):
    """The state as a runner before the state pass left it: nothing protected."""
    with Stack(estate) as st:
        for k in keys:
            infra.unprotect(st, urn(k))


def of_sessions(sessions):
    """Every live process whose session is one of `sessions`: pid -> its command.
    Read from /proc here, not through the code under test."""
    out = {}
    for pid in (int(d) for d in os.listdir("/proc") if d.isdigit()):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
            if cmd and os.getsid(pid) in sessions:  # a zombie has no command line
                out[pid] = cmd
        except OSError:
            continue
    return out


def test_a_kill_takes_the_plugins_too_and_the_pending_create_refuses_the_next_deploy(lab, monkeypatch):
    """F1 and F2. A create is in flight in the provider (an RSA key that takes
    minutes) when the run is cancelled twice. The engine does not exit, so it
    is killed: with `killpg` the provider plugin lived on, in its own process
    group, and finished the call. The state then holds `creating vm`; the next
    deploy, which names nothing, used to create vm a second time."""
    estate = lab
    monkeypatch.setattr(infra, "HARD_GRACE", 1.0)
    estate.stack(prog(web=rs(8)))
    deploy(estate)
    estate.stack(prog(web=rs(8), vm=key(16384)))
    sessions, plugins, seen = set(), {}, {}
    stop = infra.OwnedPulumi.stop

    def recording_stop(self, hard=False, grace=None):
        sessions.update(p.pid for p in self.running())  # each engine leads a session of its own
        plugins.update(of_sessions(sessions))
        stop(self, hard, grace)
    monkeypatch.setattr(infra.OwnedPulumi, "stop", recording_stop)
    ev = None

    def sink(e):
        if e["kind"] == "up":
            seen["up"] = True
        if seen.get("up") and e["kind"] == "step" and e["key"] == "vm" and "cancelled" not in seen:
            seen["cancelled"] = True

            def twice():
                time.sleep(1.0)  # the provider is in the call
                ev.cancel()
                time.sleep(0.4)
                ev.cancel()
            threading.Thread(target=twice).start()
    ev = Emitter(sink)
    t0 = time.time()
    with pytest.raises(Cancelled) as e:
        pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False), ev)
    info = e.value.result["stopped"]
    assert time.time() - t0 < 90 and info["signals"] == ["SIGINT", "SIGINT", "SIGKILL"], info
    assert info["in_flight"] == [{"op": "create", "key": "vm"}]
    # The engine had plugins in its session, in process groups of their own ...
    assert len(sessions) == 1
    tls = [pid for pid, cmd in plugins.items() if "pulumi-resource-tls" in cmd]
    assert tls and any("pulumi-language-yaml" in cmd for cmd in plugins.values()), plugins
    # ... and none of them is alive: not the provider that was in the call, not the language host.
    assert of_sessions(sessions) == {}, of_sessions(sessions)
    assert "killed, with the plugins of its session" in info["engine"] and "STILL RUNNING" not in info["engine"]
    # The message says what the state now holds and what to check.
    text = str(e.value)
    assert "pending operation for create vm" in text and "does the resource exist" in text
    assert "fleetkit adopt" in text and "pending_operations" in text
    dep = exported(estate)
    assert [(o["type"], o["resource"]["urn"]) for o in dep["pending_operations"]] == [("creating", urn("vm", KEY))]
    assert "vm" not in state(estate) and len(locks(estate)) == 1
    # The operator removes the stale lock, as the message says (`pulumi cancel`
    # does exactly this). An unattended deploy and a preview are refused, by name.
    for lock in locks(estate):
        os.unlink(lock)
    estate.calls.clear()
    estate.stack(prog(web=rs(8), vm=key(2048)))
    for preview in (True, False):
        with pytest.raises(guard.GuardError) as refused:
            deploy(estate, preview=preview)
        assert f"creating vm ({urn('vm', KEY)})" in str(refused.value)
        assert "1 pending operation(s)" in str(refused.value) and "second guest" in str(refused.value)
    assert not [c for c in estate.calls if c[0] in ("up", "preview")] and "vm" not in state(estate)
    # It does not exist (the key was never made): the pending operation is
    # removed the way the message says, and the deploy creates it, once.
    with Stack(estate) as st:
        d = st.export_stack()
        d.deployment["pending_operations"] = []
        st.import_stack(d)
    deploy(estate)
    assert "vm" in state(estate) and not exported(estate).get("pending_operations")
    no_cancel(estate)


def test_first_contact_with_a_state_nothing_protects(lab, monkeypatch):
    """F3. `protect` in the program that runs does not stop the replace of a
    resource the state does not protect yet (Pulumi 3.247). With the plan
    check out of the way, the engine alone must refuse."""
    estate = lab
    estate.stack(prog(web=rs(8), other=rs(4)))
    deploy(estate)
    unprotect_in_state(estate, "web", "other")
    before = state(estate)
    assert not before["web"].get("protect") and not before["other"].get("protect")
    # A preview writes nothing.
    estate.stack(prog(web=rs(9), other=rs(4)))
    out = deploy(estate, preview=True)
    assert out["refused"]["mini-guests"][0]["flag"] == "--allow-replace web"
    assert not state(estate)["web"].get("protect")
    # A deploy, with the refusals (and so the tripwire) switched off.
    monkeypatch.setattr(guard, "refusals", lambda *a, **k: [])
    estate.calls.clear()
    with pytest.raises(infra.EngineError, match="marked for protection"):
        deploy(estate)
    after = state(estate)
    assert after["web"]["id"] == before["web"]["id"] and len(after["web"]["id"]) == 8
    assert after["web"]["protect"] is True and after["other"]["protect"] is True
    protects = [c[2] for c in estate.calls if c[:2] == ["state", "protect"]]
    assert sorted(protects) == sorted([urn("web"), urn("other")]) and not [c for c in estate.calls if c[0] == "up"]


def test_a_named_delete_whose_up_fails_leaves_the_guests_protected(lab):
    """F6. web and other are named for a delete and unprotected in state; the
    up fails on a create before it gets to them."""
    estate = lab
    estate.stack(prog(web=rs(8), other=rs(4), third=rs(5)))
    deploy(estate)
    bad = {"type": CERT, "properties": {"privateKeyPem": "garbage", "validityPeriodHours": 1,
                                        "allowedUses": ["server_auth"], "subject": {"commonName": "x"}}}
    estate.stack(prog(third=rs(5), bad=bad))
    estate.calls.clear()
    events = []
    with pytest.raises(infra.EngineError, match="Failed to parse private key PEM"):
        deploy(estate, events, allow_delete=["other", "web"])
    st = state(estate)
    assert "web" in st and "other" in st                      # the deletes did not happen
    kinds = [(e["kind"], e["urn"].rsplit("::", 1)[-1]) for e in events if e["kind"] in ("unprotect", "protect")]
    assert sorted(kinds[:2]) == [("unprotect", "other"), ("unprotect", "web")]
    assert sorted(kinds[2:]) == [("protect", "other"), ("protect", "web")]
    assert st["web"]["protect"] is True and st["other"]["protect"] is True and st["third"]["protect"] is True
    assert not [e for e in events if e["kind"] == "error"]
    # What comes next, naming nothing: refused by name, and behind that by the engine.
    estate.stack(prog(third=rs(5)))
    with pytest.raises(guard.GuardError, match="delete of guest (web|other) .*--allow-delete"):
        deploy(estate)
    assert {"web", "other"} <= set(state(estate))
    deploy(estate, allow_delete=["other", "web"])
    assert set(state(estate)) == {"third"}
    no_cancel(estate)


def test_adoption_ids_that_name_one_resource_twice(lab):
    """F4. Two keys of one program with one id; and an id that the state of
    another stack of the estate holds."""
    estate = lab
    ev = Emitter(lambda e: None)
    estate.stack(prog(a=rs(8), b=rs(8)), adopt_ids={"a": "abcdefgh", "b": "abcdefgh"})
    report = adopt.run(estate.s, "mini", ev, apply=True)
    res = {r["key"]: r for r in report["stacks"]["mini-guests"]["resources"]}
    assert res["a"]["status"] == res["b"]["status"] == "duplicate" and res["a"]["same"] == ["b"]
    assert not report["applied"] and not report["ok"] and state(estate) == {}
    assert not [c for c in estate.calls if c[0] in ("up", "preview")]
    # One of them: adopted.
    estate.stack(prog(a=rs(8)), adopt_ids={"a": "abcdefgh"})
    report = adopt.run(estate.s, "mini", ev, apply=True)
    assert report["applied"] and report["ok"] and state(estate)["a"]["id"] == "abcdefgh"
    # The guest "moves" to another stack of the estate, which declares it with
    # the same id; the first stack still has it in state.
    estate.stack(prog())
    estate.stack(prog("mini-two", web=rs(8)), adopt_ids={"web": "abcdefgh"})
    estate.calls.clear()
    report = adopt.run(estate.s, "mini", ev, apply=True, stacks=["mini-two"])
    (r,) = report["stacks"]["mini-two"]["resources"]
    assert r["status"] == "duplicate" and r["twinStack"] == "mini-guests" and r["twin"] == urn("a")
    assert not report["applied"] and not report["ok"] and state(estate, "mini-two") == {}
    assert not [c for c in estate.calls if c[0] in ("up", "preview")]
    # A deploy of both does not offer the flag that would destroy it either:
    # mini-guests plans the delete of `a`, which is refused as an ordinary
    # unnamed delete (nothing else holds its id, since the import was refused).
    with pytest.raises(guard.GuardError, match="delete of guest a .*needs --allow-delete a"):
        deploy(estate, stacks=["mini-guests"])
    assert state(estate)["a"]["id"] == "abcdefgh"


def test_a_guest_two_state_entries_hold_is_not_offered_for_a_delete(lab):
    """F4 (d). The state as an adoption before these checks could leave it:
    one id under two keys. Dropping one key plans a delete of that entry."""
    estate = lab
    ev = Emitter(lambda e: None)
    estate.stack(prog(a=rs(8)), adopt_ids={"a": "abcdefgh"})
    assert adopt.run(estate.s, "mini", ev, apply=True)["applied"]
    with Stack(estate) as st:  # the second entry, written into the state directly
        d = st.export_stack()
        a = next(r for r in d.deployment["resources"] if r["urn"] == urn("a"))
        d.deployment["resources"].append({**a, "urn": urn("b")})
        st.import_stack(d)
    assert state(estate)["b"]["id"] == "abcdefgh"
    for kw in ({}, {"allow_delete": ["b"]}):
        with pytest.raises(guard.GuardError) as e:
            deploy(estate, **kw)
        assert "delete of guest b" in str(e.value) and f"is also in the state of mini-guests as {urn('a')}" in str(e.value)
        assert "--allow-delete" not in str(e.value) and "pulumi state delete" in str(e.value)
    assert set(state(estate)) == {"a", "b"}
    # What the refusal says to do: the entry goes, nothing real is touched, and the deploy is clean.
    with Stack(estate) as st:
        infra.unprotect(st, urn("b"))
        st._run_pulumi_cmd_sync(["state", "delete", urn("b"), "--yes"])
    out = deploy(estate)
    assert out["plan"]["mini-guests"] == [] and state(estate)["a"]["id"] == "abcdefgh"


def test_an_adoption_does_not_change_the_protect_of_what_it_depends_on(lab):
    """F7. b is adopted and depends on a, which is in state unprotected (an
    older runner's state): a is a target of the adoption's up, and the
    temporary program used to write `protect` into its state."""
    estate = lab
    ev = Emitter(lambda e: None)
    estate.stack(prog(a=rs(8), c=rs(5)))
    deploy(estate)
    unprotect_in_state(estate, "a")
    before = state(estate)
    assert not before["a"].get("protect") and before["c"]["protect"] is True
    estate.stack(prog(a=rs(8), c=rs(5), b=rs(8, dependsOn=["${a}", "${c}"])), adopt_ids={"b": "abcdefgh"})
    estate.calls.clear()
    report = adopt.run(estate.s, "mini", ev, apply=True)
    assert report["applied"] and report["ok"], report
    (up,) = [c for c in estate.calls if c[0] == "up"]
    assert urn("a") in up and urn("c") in up  # targets of the up, both
    after = state(estate)
    assert not after["a"].get("protect") and after["c"]["protect"] is True
    same = ("urn", "id", "type", "inputs", "outputs", "protect", "created")  # not where the program stood
    assert {k: after["a"].get(k) for k in same} == {k: before["a"].get(k) for k in same}
    assert (after["b"]["id"], after["b"]["protect"]) == ("abcdefgh", True)
    assert not [c for c in estate.calls if c[:2] in (["state", "protect"], ["state", "unprotect"])]


def test_a_guest_declared_as_existing_is_not_created_unless_named(lab):
    """F10. Two guests are declared with adoption ids; one is adopted, the
    other could not be (its import fails). The real program's plan creates
    the second: over a guest that exists."""
    estate = lab
    ev = Emitter(lambda e: None)
    ids = {"a": "abcdefgh", "b": "ijklmnop"}
    estate.stack(prog(a=rs(8), b=rs(8), fresh=rs(5)), adopt_ids=ids)
    report = adopt.run(estate.s, "mini", ev, apply=True, resources=["a"])
    assert report["applied"] and state(estate)["a"]["id"] == "abcdefgh"
    estate.calls.clear()
    out = deploy(estate, preview=True)
    assert {c["key"]: c["op"] for c in out["plan"]["mini-guests"]} == {"b": "create", "fresh": "create"}
    assert [(r["key"], r["flag"], r["id"]) for r in out["refused"]["mini-guests"]] == [("b", "--allow-create b", "ijklmnop")]
    with pytest.raises(guard.GuardError) as e:
        deploy(estate)
    assert "create of guest b" in str(e.value) and "declared as already existing (ijklmnop)" in str(e.value)
    assert "pass --allow-create b" in str(e.value) and "fresh" not in str(e.value)
    assert not [c for c in estate.calls if c[0] == "up"] and set(state(estate)) == {"a"}
    # Adopted instead: nothing left to refuse, and the guest that has no
    # adoption id is created as ever.
    assert adopt.run(estate.s, "mini", ev, apply=True)["applied"]
    out = deploy(estate)
    st = state(estate)
    assert st["b"]["id"] == "ijklmnop" and len(st["fresh"]["id"]) == 5 and out["refused"] == {}
    # Or named: created.
    estate.stack(prog(a=rs(8), b=rs(8), fresh=rs(5), c=rs(6)), adopt_ids={**ids, "c": "qrstuv"})
    with pytest.raises(guard.GuardError, match="pass --allow-create c"):
        deploy(estate)
    deploy(estate, allow_create=["mini-guests/c"])
    assert len(state(estate)["c"]["id"]) == 6 and state(estate)["c"]["id"] != "qrstuv"
    no_cancel(estate)
