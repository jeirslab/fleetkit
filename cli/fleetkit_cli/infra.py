"""Infra stage: run a rendered Pulumi project through the Automation API.

The Automation API drives the pulumi CLI underneath, but gives structured
engine events (per resource step, diagnostics, the summary), which is what an
API caller needs to follow a deploy. Secrets are never shown: up() defaults to
show_secrets=True, so it is passed explicitly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pulumi import automation as auto

from .events import Emitter
from .settings import Settings


def _stack(s: Settings, wd: Path) -> auto.Stack:
    opts = auto.LocalWorkspaceOptions(
        work_dir=str(wd),
        env_vars=s.pulumi_env(),
        secrets_provider="passphrase",
        pulumi_home=str(s.pulumi_home) if s.pulumi_home else None,
    )
    return auto.create_or_select_stack(stack_name=s.stack, work_dir=str(wd), opts=opts)


def _on_event(ev: Emitter, stack: str):
    def handle(e: auto.EngineEvent) -> None:
        if e.resource_pre_event:
            m = e.resource_pre_event.metadata
            if m.op != auto.OpType.SAME:
                ev.emit("infra", "step", stack=stack, op=str(m.op.value), urn=m.urn, type=m.type)
        elif e.res_outputs_event:
            m = e.res_outputs_event.metadata
            if m.op != auto.OpType.SAME:
                ev.emit("infra", "done-step", stack=stack, op=str(m.op.value), urn=m.urn)
        elif e.res_op_failed_event:
            m = e.res_op_failed_event.metadata
            ev.emit("infra", "failed-step", stack=stack, op=str(m.op.value), urn=m.urn)
        elif e.diagnostic_event and e.diagnostic_event.severity in ("error", "warning"):
            d = e.diagnostic_event
            ev.emit("infra", "diagnostic", stack=stack, severity=d.severity,
                     urn=d.urn, message=d.message.strip())
    return handle


def run(s: Settings, wd: Path, stack: str, ev: Emitter, preview: bool,
        refresh: bool = False, targets: list[str] | None = None) -> dict[str, Any]:
    ev.check()
    st = _stack(s, wd)
    ev.emit("infra", "install", stack=stack)
    st.workspace.install()
    # A cancel asks the engine to stop at the next safe point; state stays
    # consistent (Pulumi's own cancel).
    remove = ev.on_cancel(st.cancel)
    try:
        ev.check()
        common: dict[str, Any] = dict(
            on_output=lambda line: ev.emit("infra", "log", stack=stack, line=line.rstrip("\n")),
            on_event=_on_event(ev, stack),
            color="never",
            diff=True,
            refresh=refresh or None,
            target=targets or None,
        )
        ev.emit("infra", "preview" if preview else "up", stack=stack)
        if preview:
            res = st.preview(**common)
            changes = dict(res.change_summary)
        else:
            res = st.up(show_secrets=False, **common)
            changes = dict(res.summary.resource_changes or {})
    finally:
        remove()
    changes = {str(getattr(k, "value", k)): v for k, v in changes.items()}
    ev.emit("infra", "summary", stack=stack, changes=changes)
    return changes
