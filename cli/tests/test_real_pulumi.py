"""The high findings against the REAL Pulumi engine, offline: a file backend
in a temp dir and the `random` and `tls` providers that ship beside the
binary (no network, no credentials, no infrastructure). Skipped, with a
SKIPPED line saying why, where the binary or the providers are missing.

RandomString stands in for a guest: every change of it is a replacement, and
it imports by id. What these prove is the engine's side of the runner: update
plans, protect, signals and the lock, the passphrase salt across directories,
import events. What they cannot prove is in docs/pulumi.md ("Not tested")."""
import glob
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from fleetkit_cli import adopt, guard, guests, infra, pipeline, render
from fleetkit_cli.events import Cancelled, Emitter
from fleetkit_cli.pipeline import DeployRequest

RS = "random:index/randomString:RandomString"
PW = "random:index/randomPassword:RandomPassword"
RID = "random:index/randomId:RandomId"
KEY = "tls:index/privateKey:PrivateKey"


def prog(**res):
    return {"name": "mini-guests", "runtime": "yaml", "resources": res}


def rs(length, **options):
    return {"type": RS, "properties": {"length": length}, **({"options": options} if options else {})}


@pytest.fixture
def lab(real, monkeypatch):
    """RandomString is a guest here, for the guard, protect and adopt."""
    both = frozenset({*guests.GUEST_TYPES, RS})
    monkeypatch.setattr(guests, "GUEST_TYPES", both)
    monkeypatch.setattr(guard, "GUEST_TYPES", both)
    real.calls = []
    run = infra.OwnedPulumi.run

    def recording(self, args, *a, **kw):
        real.calls.append(list(args))
        return run(self, args, *a, **kw)
    monkeypatch.setattr(infra.OwnedPulumi, "run", recording)
    return real


def deploy(estate, events=None, **req):
    ev = Emitter((events if events is not None else []).append)
    return pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False, **req), ev)


def state(estate):
    """urn name -> the state resource, read with the real CLI from a fresh directory."""
    ev = Emitter(lambda e: None)
    with render.Run(estate.s, "render") as r:
        p = render.render(estate.s, "mini-guests", estate.stacks["mini-guests"], ev, r)
        st = infra.open_stack(estate.s, p.wd, "mini-guests", env(estate), ev, install=False)
        dep = st.export_stack().deployment
    out = {x["urn"].rsplit("::", 1)[-1]: x for x in dep.get("resources") or []}
    out["__salt"] = dep.get("secrets_providers", {}).get("state", {}).get("salt")
    return out


def env(estate):
    return {**estate.s.base_env(), "PULUMI_BACKEND_URL": f"file://{estate.s.state_dir / 'st'}"}


def locks(estate):
    return glob.glob(str(estate.s.state_dir / "st" / ".pulumi" / "locks" / "**" / "*.json"), recursive=True)


def no_cancel(estate):
    """`pulumi cancel` was never run, and every `up` carried --plan and --skip-preview."""
    assert not [c for c in estate.calls if c and c[0] == "cancel"]
    ups = [c for c in estate.calls if c and c[0] == "up"]
    assert all("--plan" in c for c in ups)
    return ups


def test_plan_bound_deploys_and_the_guard_layers(lab):
    """F1(a), F1(c), F5 against the real engine: create, a refused and a named
    replace, a named delete of a guest the state protects."""
    estate = lab
    estate.stack(prog(web=rs(8), other=rs(4)))
    out = deploy(estate)
    assert out["infra"]["mini-guests"]["create"] == 3 and out["applied"] == ["mini-guests"]
    before = state(estate)
    assert before["web"]["protect"] is True and before["other"]["protect"] is True  # the run protected them
    assert len(no_cancel(estate)) == 1 and locks(estate) == []
    # The settings file (the passphrase salt) was kept for later runs.
    kept = render.settings_file(estate.s, "mini-guests")
    assert "encryptionsalt" in kept.read_text() and estate.runs() == []

    # A replacement of a guest that is not named: the preview fails in the
    # engine on protect, and the guard still names what is refused.
    estate.stack(prog(web=rs(9), other=rs(4)))
    with pytest.raises(guard.GuardError, match="replace of guest web .*--allow-replace web") as e:
        deploy(estate)
    plan = {c["key"]: c for c in e.value.result["plan"]["mini-guests"]}
    assert plan["web"]["op"] == "replace" and plan["web"]["protected"] and plan["web"]["replaceReasons"] == ["length"]
    assert state(estate)["web"]["id"] == before["web"]["id"] and len(no_cancel(estate)) == 1
    out = deploy(estate, preview=True)
    assert out["refused"]["mini-guests"][0]["flag"] == "--allow-replace web"

    # Named, with its stack: replaced, by an up bound to the plan.
    out = deploy(estate, allow_replace=["mini-guests/web"])
    after = state(estate)
    assert after["web"]["id"] != before["web"]["id"] and len(after["web"]["id"]) == 9
    assert after["other"]["id"] == before["other"]["id"] and len(no_cancel(estate)) == 2
    assert after["__salt"] == before["__salt"]  # one salt, from every run's own directory

    # Removed from the program: the state protects it, so the engine's preview
    # fails; refused by name; named, it is unprotected in state and deleted.
    estate.stack(prog(web=rs(9)))
    with pytest.raises(guard.GuardError, match="delete of guest other .*--allow-delete other"):
        deploy(estate)
    assert "other" in state(estate)
    deploy(estate, allow_delete=["other"])
    assert "other" not in state(estate) and "web" in state(estate)
    assert [c for c in estate.calls if c[:2] == ["state", "unprotect"]]
    no_cancel(estate)


def test_a_program_changed_after_the_plan_is_refused_by_the_engine(lab):
    """F1(a): the up is bound to the plan inside the engine. With the runner's
    own checks out of the way (the program is rewritten behind them), Pulumi
    refuses the changed program; and it prints a secret value doing so, which
    never reaches an event or the error."""
    estate = lab
    v1 = prog(pw={"type": PW, "properties": {"length": 16}},
              web={"type": RS, "properties": {"length": 8, "keepers": {"p": "${pw.result}"}}})
    estate.stack(v1)
    deploy(estate)
    before = state(estate)
    password = json.loads(before["pw"]["outputs"]["result"]["plaintext"])
    assert len(password) == 16

    def unprotected_replace(program):  # another length, protect taken off: as a wrong render would
        program["resources"]["web"]["properties"]["length"] = 9
        program["resources"]["web"]["options"]["protect"] = False

    def other_keepers(program):  # a replacement through a property that holds a secret
        program["resources"]["web"]["properties"]["keepers"]["q"] = "x"

    for change, says in ((unprotected_replace, "protect changed"), (other_keepers, "=~keepers [values withheld]")):
        events = []
        ev = Emitter(events.append)
        with render.Run(estate.s, "deploy") as r:
            p = render.render(estate.s, "mini-guests", estate.stacks["mini-guests"], ev, r)
            planned = infra.plan(estate.s, p.wd, "mini-guests", env(estate), ev, False)
            assert planned["plan"] == [] and planned["bound"] and planned["refused"] == []
            plan_file = Path(planned["bound"]["plan_file"])
            # Secrets are encrypted in the plan file.
            assert "ciphertext" in plan_file.read_text() and password not in plan_file.read_text()
            changed = json.loads((p.wd / "Pulumi.yaml").read_text())
            change(changed)
            (p.wd / "Pulumi.yaml").write_text(json.dumps(changed))
            # 1. the runner refuses: the directory is not what was planned;
            with pytest.raises(guard.GuardError, match="changed between the plan and the up"):
                infra.apply(estate.s, p.wd, "mini-guests", env(estate), ev, planned)
            with pytest.raises(guard.GuardError, match="program changed between the plan and the up"):
                p.verify()
            # 2. and behind that, the engine does: the up itself, on the plan file.
            assert plan_file.is_file()
            st = infra.open_stack(estate.s, p.wd, "mini-guests", env(estate), ev, install=False)
            with pytest.raises(guard.GuardError, match="refused by the engine: it left the plan") as e:
                infra.engine(st, ev, "mini-guests", "up", guard.Plan(estate.s.stack, changed), plan_file=plan_file)
        assert "violates plan" in str(e.value) and says in str(e.value) and "Nothing was applied" in str(e.value)
        assert e.value.result["stopped"]["completed"] == []
        # The engine prints the changed property values, the password among
        # them, in plain text: not in the error, not in any event or log line.
        assert password not in str(e.value) and password not in json.dumps(events)
        after = state(estate)
        assert after["web"]["id"] == before["web"]["id"] and len(after["web"]["id"]) == 8
    no_cancel(estate)


def test_a_cancel_stops_the_engine_and_leaves_the_lock(lab, monkeypatch):
    """F1(b): `pulumi cancel` deleted the lock and the engine ran on. A cancel
    signals the pulumi process: the step in flight finishes, no other starts,
    the engine releases its own lock, and the result says what was done."""
    estate = lab
    n = 8
    estate.stack(prog(**{f"slow{i}": {"type": KEY, "properties": {"algorithm": "RSA", "rsaBits": 4096},
                                      "options": {"dependsOn": [f"${{slow{i - 1}}}"]} if i else {}}
                         for i in range(n)}))
    seen = {}
    stop = infra.OwnedPulumi.stop

    def recording_stop(self, hard=False, grace=None):
        stop(self, hard, grace)
        # Right after the signal: the engine still runs, and its lock is still there.
        seen.setdefault("after_signal", (bool(self.running()), len(locks(estate))))
    monkeypatch.setattr(infra.OwnedPulumi, "stop", recording_stop)
    events, out = [], {}
    ev = None

    def sink(e):
        events.append(e)
        if e["kind"] == "up":
            seen["up"] = True  # the preview has the same steps: cancel in the up
        if seen.get("up") and e["kind"] == "done-step" and e["key"] == "slow0" and "cancelled" not in seen:
            seen["cancelled"] = len(locks(estate))
            threading.Thread(target=ev.cancel).start()  # as the API's cancel does, from another thread
    ev = Emitter(sink)

    def run():
        try:
            out["result"] = pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False), ev)
        except Cancelled as e:
            out["cancelled"] = e
        except Exception as e:  # noqa: BLE001
            out["error"] = e
    t0 = time.time()
    t = threading.Thread(target=run)
    t.start()
    t.join(240)
    assert not t.is_alive() and "cancelled" in out, out
    e = out["cancelled"]
    info = e.result["stopped"]
    done = [x["key"] for x in info["completed"] if x["key"].startswith("slow")]
    assert seen["cancelled"] == 1                      # the lock was held when the cancel came
    running, held = seen["after_signal"]
    assert held == 1 or not running                    # fleetkit did not remove it
    assert info["signals"] == ["SIGINT"] and "stop after the step in flight" in info["engine"]
    assert "slow0" in done and len(done) < n and info["partly_applied"] and info["in_flight"] == []
    assert f"{len(info['completed'])} step(s) completed" in str(e) and "may be partly applied" in str(e)
    # The engine stopped: what the state has is exactly what the result says was done.
    assert sorted(k for k in state(estate) if k.startswith("slow")) == sorted(done)
    assert locks(estate) == []                         # released by the engine itself, on its way out
    no_cancel(estate)
    assert e.result["plan"]["mini-guests"] and time.time() - t0 < 200
    # The stack is usable at once: no stale lock, the next deploy finishes the rest.
    deploy(estate)
    assert len([k for k in state(estate) if k.startswith("slow")]) == n


def test_a_second_cancel_kills_an_engine_that_does_not_terminate(lab, monkeypatch):
    """With a provider call in flight the engine does not exit on the second
    SIGINT either (it prints "terminating" and waits). Its process group is
    then killed; the lock is left as it is and the result says so."""
    estate = lab
    monkeypatch.setattr(infra, "HARD_GRACE", 2.0)
    bits = lambda i: 8192 if i else 2048  # noqa: E731 - slow1 takes long enough to be in flight
    estate.stack(prog(**{f"slow{i}": {"type": KEY, "properties": {"algorithm": "RSA", "rsaBits": bits(i)},
                                      "options": {"dependsOn": [f"${{slow{i - 1}}}"]} if i else {}}
                         for i in range(3)}))
    seen = {}
    ev = None

    def sink(e):
        if e["kind"] == "up":
            seen["up"] = True
        if seen.get("up") and e["kind"] == "step" and e["key"] == "slow1" and "cancelled" not in seen:
            seen["cancelled"] = True

            def twice():
                ev.cancel()
                time.sleep(0.5)
                ev.cancel()
            threading.Thread(target=twice).start()
    ev = Emitter(sink)
    t0 = time.time()
    with pytest.raises(Cancelled) as e:
        pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False), ev)
    info = e.value.result["stopped"]
    assert time.time() - t0 < 60 and info["signals"][:2] == ["SIGINT", "SIGINT"]
    assert [x["key"] for x in info["completed"] if x["key"].startswith("slow")] == ["slow0"]
    st = state(estate)
    assert "slow0" in st and "slow2" not in st
    no_cancel(estate)
    if info["signals"][-1] == "SIGKILL":
        # Killed: the step in flight is reported as such, and the lock is still there, untouched.
        assert info["in_flight"] == [{"op": "create", "key": "slow1"}] and "slow1" not in st
        assert "killed" in info["engine"] and "lock" in info["engine"] and "pulumi cancel" in info["engine"]
        assert "whether they happened is not known" in str(e.value) and len(locks(estate)) == 1
    else:
        # The key happened to be made in time: the engine left by itself and took its lock along.
        assert locks(estate) == []


def test_two_renders_do_not_affect_each_other(lab, monkeypatch):
    """F2: between this deploy's plan and its up, the estate moves on and is
    rendered and previewed again (another job, `fleetkit preview`, an
    adoption). The deploy applies what it planned, from its own directory."""
    estate = lab
    v1 = prog(web=rs(8), pw={"type": PW, "properties": {"length": 12}})
    v2 = prog(web=rs(8), pw={"type": PW, "properties": {"length": 12}}, evil=rs(5))
    estate.stack(v1)
    real_apply, others = infra.apply, []

    def apply(s, wd, *a, **kw):
        if not others:
            estate.stack(v2)
            others.append(deploy(estate, preview=True))
            run = render.Run(estate.s, "render", keep=True)
            others.append(render.render(estate.s, "mini-guests", estate.stacks["mini-guests"],
                                        Emitter(lambda e: None), run))
            assert json.loads((wd / "Pulumi.yaml").read_text())["resources"].keys() == v1["resources"].keys()
            assert others[1].wd != wd and "evil" in (others[1].wd / "Pulumi.yaml").read_text()
        return real_apply(s, wd, *a, **kw)
    monkeypatch.setattr(infra, "apply", apply)
    out = deploy(estate)
    assert [c["key"] for c in others[0]["plan"]["mini-guests"]].count("evil") == 1  # the other run did see v2
    st = state(estate)
    assert "web" in st and "pw" in st and "evil" not in st
    assert out["program_sha256"]["mini-guests"] != others[0]["program_sha256"]["mini-guests"]
    salt = st["__salt"]
    # Secrets (the password in state) decrypt from every later directory, with
    # the one kept settings file: a deploy of v2 from a new directory works,
    # and the state keeps its salt.
    monkeypatch.setattr(infra, "apply", real_apply)
    deploy(estate)
    st2 = state(estate)
    assert "evil" in st2 and st2["__salt"] == salt and st2["pw"]["id"] == st["pw"]["id"]
    assert render.settings_file(estate.s, "mini-guests").read_text().count("encryptionsalt") == 1
    no_cancel(estate)


def test_adopt_with_the_real_engine(lab):
    """F4 and the report, with real import events: a clean import bound to
    its plan, an id already in state under another key refused, a failing
    import that does not cut the others short."""
    estate = lab
    events = []
    ev = Emitter(events.append)
    estate.stack(prog(base=rs(8)))
    deploy(estate)
    estate.stack(prog(base=rs(8), web=rs(8), bad={"type": RID, "properties": {"byteLength": 4}}, db=rs(9)),
                 adopt_ids={"web": "abcdefgh", "bad": "!!not-base64!!", "db": "abcdefghi"})
    report = adopt.run(estate.s, "mini", ev)
    res = {r["key"]: r for r in report["stacks"]["mini-guests"]["resources"]}
    # The engine stops at the import that fails; the others are compared in a second preview.
    assert res["bad"]["status"] == "error" and "decoding" in res["bad"]["error"]
    assert res["web"]["status"] == "import" and res["db"]["status"] == "import" and not report["ok"]
    assert len([c for c in estate.calls if c[0] == "preview"]) >= 2
    # Without the one that cannot be imported: adopted, verified, protected, bound to the plan.
    estate.stack(prog(base=rs(8), web=rs(8), db=rs(9)), adopt_ids={"web": "abcdefgh", "db": "abcdefghi"})
    estate.calls.clear()
    report = adopt.run(estate.s, "mini", ev, apply=True)
    assert report["ok"] and report["applied"], report
    assert report["stacks"]["mini-guests"]["verify"] == {"ok": True, "differs": [], "destroys": []}
    st = state(estate)
    assert (st["web"]["id"], st["web"]["importID"], st["web"]["protect"]) == ("abcdefgh", "abcdefgh", True)
    (up,) = no_cancel(estate)
    assert "--target" in up and "--plan" in up
    assert not glob.glob(os.path.join(tempfile.gettempdir(), f"{adopt.TEMP_PREFIX}{os.getpid()}-*"))
    # No program with `import` is left anywhere (Pulumi's own backend, its update history, aside).
    backend = str(estate.s.state_dir / "st")
    assert [f for f in estate.import_anywhere() if not f.startswith(backend)] == [] and estate.runs() == []
    # The key is renamed (web -> web2) and declared again with the same id:
    # refused, the state keeps one entry for it, and nothing is imported.
    estate.stack(prog(base=rs(8), web2=rs(8), db=rs(9)), adopt_ids={"web2": "abcdefgh"})
    estate.calls.clear()
    report = adopt.run(estate.s, "mini", ev, apply=True)
    (r,) = report["stacks"]["mini-guests"]["resources"]
    assert r["status"] == "duplicate" and r["twin"].endswith("::web") and "pulumi state rename" in r["why"]
    assert not report["ok"] and not report["applied"] and not [c for c in estate.calls if c[0] in ("up", "preview")]
    st = state(estate)
    assert "web" in st and "web2" not in st
    # What a deploy of that program would do to the live resource: refused by the guard.
    with pytest.raises(guard.GuardError, match="delete of guest web"):
        deploy(estate)
    # A declaration that differs: for this provider a replacement, which adopt never does.
    estate.stack(prog(base=rs(8), web=rs(8), db=rs(9), w3=rs(7)), adopt_ids={"w3": "abcdefghijk"})
    report = adopt.run(estate.s, "mini", ev, apply=True)
    (r,) = report["stacks"]["mini-guests"]["resources"]
    assert r["status"] == "other" and "replace" in r["steps"] and not report["applied"]
    d = {x["path"]: x for x in r["diff"]}["length"]
    assert (d["live"], d["declared"], d["kind"]) == (11, 7, "real")
    assert "w3" not in state(estate)


def test_the_engines_detailed_diff_reaches_the_plan(lab, tmp_path):
    """R1: the engine writes `detailedDiff` (and then no `diffs`); the pinned
    SDK reads `detailed_diff` and so drops it. With infra.py's correction the
    plan has the nested path and what forces the replacement. The kubernetes
    provider, rendering YAML to a directory, is the offline provider that
    sends one (`random` and `tls` send none)."""
    import shutil
    exe = shutil.which("pulumi")
    if not (shutil.which("pulumi-resource-kubernetes")
            or os.path.exists(os.path.join(os.path.dirname(os.path.realpath(exe)), "pulumi-resource-kubernetes"))):
        pytest.skip("real Pulumi: pulumi-resource-kubernetes is not installed (and nothing may be downloaded)")
    estate = lab

    def cm(a):
        return prog(k={"type": "pulumi:providers:kubernetes",
                       "properties": {"renderYamlToDirectory": str(tmp_path / "rendered")}},
                    cm={"type": "kubernetes:core/v1:ConfigMap", "options": {"provider": "${k}"},
                        "properties": {"metadata": {"name": "cm", "namespace": "default"}, "data": {"a": a, "b": "x"}}})
    estate.stack(cm("1"))
    deploy(estate)
    estate.stack(cm("2"))
    out = deploy(estate, preview=True)
    (c,) = out["plan"]["mini-guests"]
    assert (c["key"], c["op"]) == ("cm", "replace")
    assert c["diff"] == ["data.a"] and c["replaceReasons"] == ["data.a"]
