"""Infra stage: run a rendered Pulumi project through the Automation API.

The Automation API drives the pulumi CLI underneath, but gives structured
engine events (per resource step, diagnostics, the summary), which is what an
API caller needs to follow a deploy. Secrets are never shown: up() defaults to
show_secrets=True, so it is passed explicitly.

Every `up` is two calls: plan() previews the stack and builds the per-resource
change list the guard reads (guard.py); apply() runs the `up` only once the
pipeline has seen every stack's plan pass. apply() also watches the steps of
the `up` itself and cancels the engine if one comes up that the plan did not
allow (the state can move between the preview and the up).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from pulumi import automation as auto

from . import guard
from .events import Emitter
from .settings import Settings

# (the resource's plan entry, the step's op) -> why this step must not run, or
# None. Called before each step of an `up`.
Tripwire = Callable[[dict[str, Any], str], Optional[str]]


def _stack(s: Settings, wd: Path, env: dict[str, str]) -> auto.Stack:
    opts = auto.LocalWorkspaceOptions(
        work_dir=str(wd),
        env_vars=env,
        secrets_provider="passphrase",
        pulumi_home=str(s.pulumi_home) if s.pulumi_home else None,
    )
    return auto.create_or_select_stack(stack_name=s.stack, work_dir=str(wd), opts=opts)


def open_stack(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, install: bool = True) -> Any:
    ev.check()
    st = _stack(s, wd, env)
    if install:
        ev.emit("infra", "install", stack=stack)
        st.workspace.install()
    return st


def _on_event(ev: Emitter, stack: str, plan: guard.Plan, before_step: Callable[[dict[str, Any], str], None]):
    def handle(e: auto.EngineEvent) -> None:
        if e.resource_pre_event:
            m = e.resource_pre_event.metadata
            entry = plan.step(m)
            if m.op != auto.OpType.SAME:
                before_step(entry, str(m.op.value))
                ev.emit("infra", "step", stack=stack, op=str(m.op.value), urn=m.urn, type=m.type, key=entry["key"])
        elif e.res_outputs_event:
            m = e.res_outputs_event.metadata
            entry = plan.step(m)
            if m.op != auto.OpType.SAME:
                ev.emit("infra", "done-step", stack=stack, op=str(m.op.value), urn=m.urn, key=entry["key"])
        elif e.res_op_failed_event:
            m = e.res_op_failed_event.metadata
            entry = plan.step(m)
            plan.error(m.urn, f"{m.op.value} failed")
            ev.emit("infra", "failed-step", stack=stack, op=str(m.op.value), urn=m.urn, key=entry["key"])
        elif e.diagnostic_event and e.diagnostic_event.severity in ("error", "warning"):
            d = e.diagnostic_event
            if d.severity == "error":
                plan.error(d.urn or "", d.message.strip())
            ev.emit("infra", "diagnostic", stack=stack, severity=d.severity,
                     urn=d.urn, message=d.message.strip())
    return handle


def engine(st: Any, ev: Emitter, stack: str, verb: str, plan: guard.Plan, refresh: bool = False,
           targets: list[str] | None = None, tripwire: Tripwire | None = None) -> dict[str, int]:
    """One `preview` or `up` of an open stack; its steps go into `plan`.
    -> the engine's own counts by op."""
    tripped: list[str] = []

    def before_step(entry: dict[str, Any], op: str) -> None:
        why = tripwire(entry, op) if tripwire else None
        if why:
            if not tripped:
                # Pulumi's own cancel: the engine stops at the next safe point.
                st.cancel()
            tripped.append(why)

    # A cancel asks the engine to stop at the next safe point; state stays
    # consistent (Pulumi's own cancel).
    remove = ev.on_cancel(st.cancel)
    try:
        ev.check()
        common: dict[str, Any] = dict(
            on_output=lambda line: ev.emit("infra", "log", stack=stack, line=line.rstrip("\n")),
            on_event=_on_event(ev, stack, plan, before_step),
            color="never",
            diff=True,
            refresh=refresh or None,
            target=targets or None,
        )
        ev.emit("infra", verb, stack=stack)
        try:
            if verb == "preview":
                changes = dict(st.preview(**common).change_summary)
            else:
                changes = dict(st.up(show_secrets=False, **common).summary.resource_changes or {})
        except Exception as e:
            if tripped:
                raise guard.GuardError(_tripped(stack, tripped)) from e
            raise
    finally:
        remove()
    if tripped:
        raise guard.GuardError(_tripped(stack, tripped))
    return {str(getattr(k, "value", k)): v for k, v in changes.items()}


def _tripped(stack: str, why: list[str]) -> str:
    return (f"{stack}: the up was cancelled, it started a step its plan did not have (the stack may be "
            f"partly applied; preview it): " + "; ".join(why))


def plan(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, preview: bool,
         refresh: bool = False, targets: list[str] | None = None,
         allow: guard.Allow | None = None) -> dict[str, Any]:
    """Preview the stack. -> {changes: counts, plan: [...], refused: [...]}.
    `preview` only words the events: whether a deploy follows."""
    program = guard.load_program(wd)
    st = open_stack(s, wd, stack, env, ev)
    p = guard.Plan(s.stack, program)
    counts = engine(st, ev, stack, "preview", p, refresh, targets)
    changes = p.changes()
    refused = guard.import_refusals(program) + guard.refusals(changes, allow or guard.Allow())
    ev.emit("infra", "plan", stack=stack, preview=preview, changes=changes, refused=refused,
            text=guard.lines(stack, changes, refused, preview))
    ev.emit("infra", "summary", stack=stack, changes=counts, resources=guard.by_op(changes), refused=refused)
    return {"changes": counts, "plan": changes, "refused": refused}


def apply(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, refresh: bool = False,
          targets: list[str] | None = None, allow: guard.Allow | None = None) -> dict[str, Any]:
    """`up` a stack whose plan passed. -> {changes: counts, plan: what it did}."""
    program = guard.load_program(wd)
    if guard.imports(program):
        raise guard.GuardError(guard.message({stack: guard.import_refusals(program)}))
    allow = allow or guard.Allow()
    st = open_stack(s, wd, stack, env, ev, install=False)
    p = guard.Plan(s.stack, program)

    def tripwire(entry: dict[str, Any], op: str) -> Optional[str]:
        bad = guard.refusals([{**entry, "steps": [op]}], allow)
        return f"{op} of guest {entry['key']} (needs {bad[0]['flag']})" if bad else None

    counts = engine(st, ev, stack, "up", p, refresh, targets, tripwire)
    changes = p.changes()
    ev.emit("infra", "summary", stack=stack, changes=counts, resources=guard.by_op(changes), refused=[])
    return {"changes": counts, "plan": changes}
