"""A fake Pulumi engine for the offline tests: a stack object with the methods
infra.py calls (workspace.install, workspace.pulumi_command.stop, export_stack,
preview, up, `state unprotect`), driven by the program in its work dir, a
state and a set of live resources. It emits real pulumi.automation engine
events.

What it models, each as seen with a real engine (Pulumi 3.247, offline; see
docs/pulumi.md and test_real_pulumi.py):

- create / same / update by comparing declared inputs with the state; delete
  of what the state has and the program does not (untargeted runs only);
- `options.import` on a resource that is not in state: an `import` step (pre
  event with the declaration, outputs event with the live resource as `old`
  and as `new`), then, when the declaration differs, an `update` step with the
  live resource as `old`, the declaration as `new` and the differing keys in
  `diffs`. The inputs of what was imported leave out zero values (false, 0,
  ""); its outputs have them. A missing import id is an error diagnostic on
  the resource and ends the run there: later resources get no event;
- update plans: a preview given `plan=` writes one (unless it failed), an up
  given `plan=` refuses, before anything else, a resource whose steps are not
  the planned ones ("violates plan");
- `protect`: an up records the program's flag in state; a replacement of a
  resource the program protects, and a delete of one the state protects, are
  error diagnostics that fail the run, with the steps still in the events of
  a preview; `pulumi state unprotect`;
- stopping: `pulumi_command.stop()` (the signal infra.py sends) lets the step
  in flight finish and starts no other; `stop(hard=True)` ends the run at
  once, the step in flight not done. `cancel()` is Pulumi's `pulumi cancel`:
  it stops nothing (as on a self-managed backend) and is counted, because
  nothing may call it;
- --target as "only these URNs are looked at".

`force` scripts the steps of a resource (a replacement, say), `drift` does the
same for the up only. `ignore_plan` / `ignore_protect` switch a layer off, to
test the one behind it.

It is not Pulumi: see docs/pulumi.md ("Tested") for what this does not prove.
"""
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from pulumi import automation as auto

CT = "proxmox:index/virtualEnvironmentContainer:VirtualEnvironmentContainer"
VM = "proxmox:index/virtualEnvironmentVm:VirtualEnvironmentVm"
POOL = "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool"
PROVIDER = "pulumi:providers:proxmox"
GUESTS = (CT, VM)
SECRET_VALUE = "hunter2-violation-value"

_ZERO = (False, 0, "", None)
_MISSING = object()


def program(**resources):
    """A program in the shape lib.toPulumi renders (JSON)."""
    res = {"provider-proxmox": {"type": PROVIDER, "properties": {"endpoint": "https://pve.example:8006"}}}
    for key, r in resources.items():
        res[key] = {"type": r["type"], "properties": dict(r.get("properties") or {}),
                    "options": {"provider": "${provider-proxmox}", **(r.get("options") or {})}}
    return {"name": "mini-guests", "runtime": "yaml", "resources": res}


def urn(prog, key):
    r = prog["resources"][key]
    return f"urn:pulumi:main::{prog['name']}::{r['type']}::{r.get('name') or key}"


class World:
    def __init__(self):
        self.state = {}        # urn -> {type, id, inputs, protect?, importID?}
        self.live = {}         # (type, id) -> properties of what really exists
        self.defaults = {}     # type -> {property: the provider's default}
        self.unrecorded = {}   # (type, id) -> properties the import does not record
        self.import_error = {}  # (type, id) -> the provider's error when importing it
        self.calls = []        # every preview / up: verb, wd, imports seen, targets, plan, protect
        self.force = {}        # key -> {steps, diff, keys}: scripted steps
        self.drift = {}        # the same, for an up only
        self.fail = {}         # verb -> exception raised at the end of the run
        self.import_keeps_live = False  # an imported resource keeps its live inputs in state
        self.ignore_plan = False        # the engine does not enforce the update plan
        self.ignore_protect = False     # the engine does not enforce protect
        self.stop_after = None  # (op, key): a cancel arrives while this step is in flight
        self.on_step = None     # called with (verb, op, key) at each pre event
        self.stops = []        # "soft" / "hard": what pulumi_command.stop was asked
        self.pulumi_cancel = 0  # calls of `pulumi cancel`: must stay 0
        self.unprotected = []  # urns given to `pulumi state unprotect`
        self.destroyed = []    # keys whose real resource an up destroyed
        self.updated = []      # keys whose real resource an up changed
        self.created = []

    def in_state(self, prog, key, rid=None, protect=None):
        r = prog["resources"][key]
        self.state[urn(prog, key)] = {"type": r["type"], "id": rid or key, "inputs": dict(r.get("properties") or {})}
        if protect is not None:
            self.state[urn(prog, key)]["protect"] = protect

    def stack(self, wd):
        return FakeStack(self, Path(wd))


class FakeStack:
    def __init__(self, world, wd):
        self.w, self.wd = world, wd
        self._stop = None  # None, "soft" or "hard"
        cmd = SimpleNamespace(signals=[], outcome=None)

        def stop(hard=False, grace=None):
            self.w.stops.append("hard" if hard else "soft")
            cmd.signals.append("SIGINT")
            cmd.outcome = "told to terminate at once" if hard else "told to stop after the step in flight"
            self._stop = "hard" if hard or self._stop else "soft"

        cmd.stop = stop
        self.workspace = SimpleNamespace(install=lambda: None, pulumi_command=cmd)

    def export_stack(self):
        return SimpleNamespace(version=3, deployment={"resources": [
            {"urn": u, "type": r["type"], "id": r["id"], "inputs": r["inputs"],
             **({"importID": r["importID"]} if r.get("importID") else {}),
             **({"protect": True} if r.get("protect") else {})} for u, r in self.w.state.items()]})

    def cancel(self):
        # `pulumi cancel` on a self-managed backend: removes the lock, stops nothing.
        self.w.pulumi_cancel += 1

    def _run_pulumi_cmd_sync(self, args, *a, **kw):
        assert args[:2] == ["state", "unprotect"] and args[3:] == ["--yes"], args
        self.w.state[args[2]]["protect"] = False
        self.w.unprotected.append(args[2])

    def preview(self, **kw):
        return SimpleNamespace(change_summary=self._run("preview", kw))

    def up(self, **kw):
        return SimpleNamespace(summary=SimpleNamespace(resource_changes=self._run("up", kw)))

    # ── the engine ───────────────────────────────────────────────────────
    def _run(self, verb, kw):
        text = (self.wd / "Pulumi.yaml").read_text()
        prog = json.loads(text)
        res = prog["resources"]
        targets = kw.get("target")
        plan_file = kw.get("plan")
        self.w.calls.append({
            "verb": verb, "wd": str(self.wd), "targets": targets, "plan": plan_file,
            "program": hashlib.sha256(text.encode()).hexdigest(),
            "protect": sorted(k for k, r in res.items() if (r.get("options") or {}).get("protect") is True),
            "imports": {k: r["options"]["import"] for k, r in res.items() if "import" in (r.get("options") or {})}})
        on_event = kw.get("on_event") or (lambda e: None)
        up = verb == "up"
        planned = None
        if up and plan_file and not self.w.ignore_plan:
            planned = json.loads(Path(plan_file).read_text())["steps"]
        counts, seq, failed, recorded = {}, [0], [], {}

        def meta(op, u, type_, old=None, new=None, diffs=None, keys=None):
            def state(side):
                if side is None:
                    return None
                inputs, outputs = side if isinstance(side, tuple) else (side, None)
                return auto.StepEventStateMetadata(type=type_, urn=u, id="", parent="", provider="",
                                                   inputs=inputs, outputs=outputs)
            return auto.StepEventMetadata(op=auto.OpType(op), urn=u, type=type_, provider="",
                                          old=state(old), new=state(new), diffs=diffs, keys=keys)

        def send(**kind):
            seq[0] += 1
            on_event(auto.EngineEvent(sequence=seq[0], timestamp=int(time.time()), **kind))

        def diag(u, message):
            send(diagnostic_event=auto.DiagnosticEvent(message=message, color="never", severity="error", urn=u))

        def step(op, key, u, type_, effect=None, pre=None, **m):
            """pre event, the effect (an up only), outputs event. False: the run ends here."""
            if self._stop:
                return False  # asked to stop: no new step
            recorded.setdefault(u, []).append(op)
            send(resource_pre_event=auto.ResourcePreEvent(meta(op, u, type_, new=pre if pre is not None else m.get("new"))))
            if self.w.on_step:
                self.w.on_step(verb, op, key)
            if up and self.w.stop_after == (op, key):
                self.workspace.pulumi_command.stop()
            if self._stop == "hard":
                return False  # terminated at once: the step in flight is not done
            if up and effect:
                effect()
            send(res_outputs_event=auto.ResOutputsEvent(meta(op, u, type_, **m)))
            counts[op] = counts.get(op, 0) + 1
            return True

        def leaves_plan(u, ops):
            if planned is None or [o for o in ops if o != "same"] == planned.get(u, []):
                return False
            diag(u, f"resource {u} violates plan: properties changed: =~cores[{{{SECRET_VALUE}}}]")
            failed.append(u.rsplit("::", 1)[-1])
            return True

        def protected(r):
            return (r.get("options") or {}).get("protect") is True and not self.w.ignore_protect

        def run():
            for key, r in res.items():
                u, type_, inputs = urn(prog, key), r["type"], dict(r.get("properties") or {})
                if targets and u not in targets:
                    continue
                forced = (self.w.drift.get(key) if up else None) or self.w.force.get(key)
                st = self.w.state.get(u)
                imp = (r.get("options") or {}).get("import")
                if st is not None and up:
                    st["protect"] = (r.get("options") or {}).get("protect") is True
                if forced:
                    if leaves_plan(u, forced["steps"]):
                        return
                    if protected(r) and st is not None and any("replace" in op for op in forced["steps"]):
                        diag(u, f'unable to replace resource "{u}"\nas it is currently marked for protection. '
                                f"To unprotect the resource, remove the `protect` flag from the resource in "
                                f"your Pulumi program and run `pulumi up`")
                        failed.append(key)
                        if up:
                            continue
                    for op in forced["steps"]:
                        def effect(op=op, key=key, u=u):
                            if op in ("delete-replaced", "delete"):
                                self.w.destroyed.append(key)
                            if op == "update":
                                self.w.updated.append(key)
                        if not step(op, key, u, type_, effect, new=inputs, diffs=forced.get("diff"),
                                    keys=forced.get("keys")):
                            return
                elif st is not None:
                    diff = sorted(k for k in {*inputs, *st["inputs"]} if inputs.get(k) != st["inputs"].get(k))
                    if leaves_plan(u, ["update" if diff else "same"]):
                        return

                    def effect(key=key, st=st, inputs=inputs):
                        st["inputs"] = inputs
                        self.w.live[(st["type"], st["id"])] = dict(inputs)
                        self.w.updated.append(key)
                    if not step("update" if diff else "same", key, u, type_, effect if diff else None,
                                old=st["inputs"], new=inputs, diffs=diff or None):
                        return
                elif imp is not None:
                    live = self.w.live.get((type_, imp))
                    broken = self.w.import_error.get((type_, imp))
                    if live is None or broken:
                        recorded.setdefault(u, []).append("import")
                        send(resource_pre_event=auto.ResourcePreEvent(meta("import", u, type_, new=inputs)))
                        diag(u, broken or f"Preview failed: resource '{imp}' does not exist")
                        failed.append(key)
                        return  # the engine stops at a failed import
                    hidden = self.w.unrecorded.get((type_, imp)) or []
                    read = {k: v for k, v in live.items() if k not in hidden}
                    read_inputs = {k: v for k, v in read.items() if v not in _ZERO}
                    wanted = {**(self.w.defaults.get(type_) or {}), **inputs}
                    diff = sorted(k for k in wanted if wanted[k] != read.get(k, _MISSING))
                    if leaves_plan(u, ["import", "update"] if diff else ["import"]):
                        return
                    keep = self.w.import_keeps_live
                    sid = imp.rsplit("/", 1)[-1] if type_ in GUESTS else imp

                    def imported(u=u, type_=type_, imp=imp, sid=sid, live=live, r=r, inputs=inputs, diff=diff):
                        self.w.state[u] = {"type": type_, "id": sid, "importID": imp,
                                           "inputs": dict(live) if diff else inputs,
                                           "protect": (r.get("options") or {}).get("protect") is True}
                    if not step("import", key, u, type_, imported, pre=inputs,
                                old=(read_inputs, {**read, "id": sid}), new=(read_inputs, {**read, "id": sid})):
                        return
                    if diff:
                        def updated(key=key, u=u, type_=type_, imp=imp, live=live, inputs=inputs, wanted=wanted):
                            if not keep:
                                self.w.state[u]["inputs"] = inputs
                                self.w.live[(type_, imp)] = {**live, **wanted}
                                self.w.updated.append(key)
                        if not step("update", key, u, type_, updated, pre=inputs,
                                    old=(read_inputs, {**read, "id": sid}), new=(inputs, {**wanted, "id": sid}),
                                    diffs=diff):
                            return
                else:
                    if leaves_plan(u, ["create"]):
                        return

                    def effect(key=key, u=u, type_=type_, inputs=inputs, r=r):
                        self.w.state[u] = {"type": type_, "id": key, "inputs": inputs,
                                           "protect": (r.get("options") or {}).get("protect") is True}
                        self.w.live[(type_, key)] = dict(inputs)
                        self.w.created.append(key)
                    if not step("create", key, u, type_, effect, new=inputs):
                        return
            if not targets:
                have = {urn(prog, k) for k in res}
                for u in [u for u in self.w.state if u not in have and f"::{prog['name']}::" in u]:
                    st, key = self.w.state[u], u.rsplit("::", 1)[-1]
                    if leaves_plan(u, ["delete"]):
                        return
                    if st.get("protect") and not self.w.ignore_protect:
                        recorded.setdefault(u, []).append("delete")
                        send(resource_pre_event=auto.ResourcePreEvent(meta("delete", u, st["type"])))
                        diag(u, f'Preview failed: resource "{u}" cannot be deleted\nbecause it is protected. To '
                                f"unprotect the resource, either remove the `protect` flag from the resource in "
                                f"your Pulumi program and run `pulumi up`, or use the command:\n"
                                f"`pulumi state unprotect '{u}'`")
                        failed.append(key)
                        continue

                    def effect(u=u, key=key):
                        del self.w.state[u]
                        self.w.destroyed.append(key)
                    if not step("delete", key, u, st["type"], effect, old=st["inputs"]):
                        return

        run()
        if self._stop:
            raise RuntimeError("error: update canceled")
        if failed:
            diag("", f"{'update' if up else 'preview'} failed")
            raise RuntimeError(f"{verb} failed: {', '.join(failed)}")
        if verb in self.w.fail:
            raise self.w.fail[verb]
        if verb == "preview" and plan_file:
            Path(plan_file).write_text(json.dumps({
                "fake": True, "steps": {u: [o for o in ops if o != "same"] for u, ops in recorded.items()
                                        if [o for o in ops if o != "same"]}}))
        return counts
