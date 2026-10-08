"""A fake Pulumi engine for the offline tests: a stack object with the methods
infra.py calls (workspace.install, export_stack, preview, up, cancel), driven
by the program in its work dir, a state and a set of live resources. It emits
real pulumi.automation engine events.

What it models: create / same / update by comparing declared inputs with the
state; delete of what the state has and the program does not (untargeted runs
only); `options.import` on a resource that is not in state as an import step
whose outputs event carries the live inputs as `old` and the declaration as
`new`, with the differing keys in `diffs`, and, on an up, the live resource
updated to the declaration; a missing import id as an error diagnostic and a
failed run; --target as "only these URNs are looked at". `force` scripts the
steps of a resource (a replacement, say), `drift` does the same for the up
only. A cancel stops the run before the step that asked for it.

It is not Pulumi: see docs/pulumi.md ("Adopting what already exists") for what
this does not prove.
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

from pulumi import automation as auto

CT = "proxmox:index/virtualEnvironmentContainer:VirtualEnvironmentContainer"
VM = "proxmox:index/virtualEnvironmentVm:VirtualEnvironmentVm"
POOL = "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool"
PROVIDER = "pulumi:providers:proxmox"


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
        self.state = {}        # urn -> {type, id, inputs}
        self.live = {}         # (type, id) -> properties of what really exists
        self.calls = []        # every preview / up: verb, wd, imports seen, targets
        self.force = {}        # key -> {steps, diff, keys}: scripted steps
        self.drift = {}        # the same, for an up only
        self.fail = {}         # verb -> exception raised at the end of the run
        self.import_keeps_live = False  # an imported resource keeps its live inputs in state
        self.cancelled = 0
        self.destroyed = []    # keys whose real resource an up destroyed
        self.updated = []      # keys whose real resource an up changed
        self.created = []

    def in_state(self, prog, key, rid=None):
        r = prog["resources"][key]
        self.state[urn(prog, key)] = {"type": r["type"], "id": rid or key, "inputs": dict(r.get("properties") or {})}

    def stack(self, wd):
        return FakeStack(self, Path(wd))


class FakeStack:
    def __init__(self, world, wd):
        self.w, self.wd = world, wd
        self.workspace = SimpleNamespace(install=lambda: None)
        self._cancel = False

    def export_stack(self):
        return SimpleNamespace(version=3, deployment={"resources": [
            {"urn": u, "type": r["type"], "id": r["id"]} for u, r in self.w.state.items()]})

    def cancel(self):
        self.w.cancelled += 1
        self._cancel = True

    def preview(self, **kw):
        return SimpleNamespace(change_summary=self._run("preview", kw))

    def up(self, **kw):
        return SimpleNamespace(summary=SimpleNamespace(resource_changes=self._run("up", kw)))

    # ── the engine ───────────────────────────────────────────────────────
    def _run(self, verb, kw):
        prog = json.loads((self.wd / "Pulumi.yaml").read_text())
        res = prog["resources"]
        targets = kw.get("target")
        self.w.calls.append({
            "verb": verb, "wd": str(self.wd), "targets": targets,
            "imports": {k: r["options"]["import"] for k, r in res.items() if "import" in (r.get("options") or {})}})
        on_event = kw.get("on_event") or (lambda e: None)
        up = verb == "up"
        counts, seq, failed = {}, [0], []

        def meta(op, key, u, type_, old=None, new=None, diffs=None, keys=None):
            state = lambda i: None if i is None else auto.StepEventStateMetadata(  # noqa: E731
                type=type_, urn=u, id="", parent="", provider="", inputs=i)
            return auto.StepEventMetadata(op=auto.OpType(op), urn=u, type=type_, provider="",
                                          old=state(old), new=state(new), diffs=diffs, keys=keys)

        def send(**kind):
            seq[0] += 1
            on_event(auto.EngineEvent(sequence=seq[0], timestamp=int(time.time()), **kind))

        def step(op, key, u, type_, effect=None, **m):
            """pre event, the effect (an up only), outputs event. False: cancelled."""
            send(resource_pre_event=auto.ResourcePreEvent(meta(op, key, u, type_, new=m.get("new"))))
            if self._cancel:
                return False
            if up and effect:
                effect()
            send(res_outputs_event=auto.ResOutputsEvent(meta(op, key, u, type_, **m)))
            counts[op] = counts.get(op, 0) + 1
            return True

        def run():
            for key, r in res.items():
                u, type_, inputs = urn(prog, key), r["type"], dict(r.get("properties") or {})
                if targets and u not in targets:
                    continue
                forced = (self.w.drift.get(key) if up else None) or self.w.force.get(key)
                st = self.w.state.get(u)
                imp = (r.get("options") or {}).get("import")
                if forced:
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

                    def effect(key=key, st=st, inputs=inputs):
                        st["inputs"] = inputs
                        self.w.live[(st["type"], st["id"])] = dict(inputs)
                        self.w.updated.append(key)
                    if not step("update" if diff else "same", key, u, type_, effect if diff else None,
                                old=st["inputs"], new=inputs, diffs=diff or None):
                        return
                elif imp is not None:
                    live = self.w.live.get((type_, imp))
                    if live is None:
                        send(resource_pre_event=auto.ResourcePreEvent(meta("import", key, u, type_, new=inputs)))
                        send(diagnostic_event=auto.DiagnosticEvent(
                            message=f"Preview failed: resource '{imp}' does not exist", color="never",
                            severity="error", urn=u))
                        failed.append(key)
                        continue
                    diff = sorted(k for k in inputs if inputs[k] != live.get(k))

                    def effect(key=key, u=u, type_=type_, imp=imp, live=live, inputs=inputs, diff=diff):
                        keep = self.w.import_keeps_live
                        self.w.state[u] = {"type": type_, "id": imp, "inputs": dict(live) if keep else inputs}
                        if diff and not keep:
                            self.w.live[(type_, imp)] = {**live, **inputs}
                            self.w.updated.append(key)
                    if not step("import", key, u, type_, effect, old=live, new=inputs, diffs=diff or None):
                        return
                else:
                    def effect(key=key, u=u, type_=type_, inputs=inputs):
                        self.w.state[u] = {"type": type_, "id": key, "inputs": inputs}
                        self.w.live[(type_, key)] = dict(inputs)
                        self.w.created.append(key)
                    if not step("create", key, u, type_, effect, new=inputs):
                        return
            if not targets:
                have = {urn(prog, k) for k in res}
                for u in [u for u in self.w.state if u not in have and f"::{prog['name']}::" in u]:
                    st, key = self.w.state[u], u.rsplit("::", 1)[-1]

                    def effect(u=u, key=key):
                        del self.w.state[u]
                        self.w.destroyed.append(key)
                    if not step("delete", key, u, st["type"], effect, old=st["inputs"]):
                        return

        run()
        if self._cancel:
            raise RuntimeError("update cancelled")
        if failed:
            raise RuntimeError(f"{verb} failed: {', '.join(failed)}")
        if verb in self.w.fail:
            raise self.w.fail[verb]
        return counts
