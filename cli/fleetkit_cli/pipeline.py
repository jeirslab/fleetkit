"""One deploy: render every stack, provision with Pulumi, configure with
Colmena. The CLI and the API run exactly this.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from . import backends, guard, infra, nixos, render
from .events import Cancelled, Emitter
from .settings import Settings

_NAME = "A name is <stack>/<key>, a URN, or a key that only one stack of the request has."


class DeployRequest(BaseModel):
    estate: str = Field(description="Estate name: the programs under pulumi.<estate>.")
    stacks: Optional[list[str]] = Field(
        default=None, description="Pulumi stacks to run (default: every stack of the estate).")
    rev: Optional[str] = Field(
        default=None, description="Git commit or branch to deploy (server with a repo only; default the branch head).")
    pr: Optional[int] = Field(default=None, description="The pull request this job previews or deploys (set by GitOps).")
    infra: bool = Field(default=True, description="Run the Pulumi stage.")
    nixos: bool = Field(default=True, description="Run the Colmena stage.")
    hive: Optional[str] = Field(default=None, description="Hive name under hives.* (default: the estate).")
    on: list[str] = Field(default_factory=list, description="Colmena --on: node names or @tags.")
    goal: Literal["switch", "test", "boot", "dry-activate"] = "switch"
    preview: bool = Field(default=False, description="pulumi preview and colmena build; nothing changes.")
    refresh: bool = Field(default=False, description="Refresh Pulumi state from the providers first.")
    targets: list[str] = Field(default_factory=list, description="Pulumi --target URNs.")
    # The guard (guard.py): a deploy whose plan replaces, deletes or updates a
    # guest is refused unless the guest is named here for that op.
    allow_replace: list[str] = Field(
        default_factory=list, description="Guests this deploy may replace: destroy and create anew. " + _NAME)
    allow_delete: list[str] = Field(
        default_factory=list, description="Guests this deploy may delete. " + _NAME)
    allow_update: list[str] = Field(
        default_factory=list, description="Guests this deploy may update in place (a reboot). " + _NAME)
    allow_create: list[str] = Field(
        default_factory=list, description="Guests this deploy may create although the stack declares them as "
                                          "already existing (they have an adoption id), and HA resources it may "
                                          "create. " + _NAME)


def _names(program: dict[str, Any], state: list[dict[str, Any]]) -> set[str]:
    """What a bare allow name can mean in a stack: a key of its program, or the
    name of a resource in its state (one the program no longer has)."""
    return {*(program.get("resources") or {}), *(r["urn"].rsplit("::", 1)[-1] for r in state)}


def run(s: Settings, req: DeployRequest, ev: Emitter) -> dict[str, Any]:
    # (`unprotected.<stack>` is added when guests could not be protected in
    # state again after an up: infra.reprotect.)
    result: dict[str, Any] = {"infra": {}, "plan": {}, "refused": {}, "programs": {}, "program_sha256": {},
                              "applied": [], "nixos": None}
    # Everything this run writes goes into a directory of its own, removed at
    # the end: no other run (a job, a preview, an adoption) writes there, so
    # what is applied is what was previewed here.
    with render.Run(s, "preview" if req.preview else "deploy") as rundir:
        try:
            if req.infra:
                _infra(s, req, ev, rundir, result)
            if req.nixos:
                # After infra: the hive is evaluated now, against what was provisioned.
                nixos.run(s, req.hive or req.estate, ev, req.goal, req.preview, req.on, rundir.dir)
                result["nixos"] = "built" if req.preview else req.goal
        except (guard.GuardError, Cancelled, infra.EngineError) as e:
            # A refused, cancelled or failed deploy still says what it planned
            # and, if an up had started, exactly how far it got (`stopped`).
            e.result = {**result, **(getattr(e, "result", None) or {})}  # type: ignore[union-attr]
            raise
    return result


def _infra(s: Settings, req: DeployRequest, ev: Emitter, rundir: render.Run, result: dict[str, Any]) -> None:
    stacks = render.stacks_of(s, req.estate, req.stacks)
    allow = guard.Allow(replace=req.allow_replace, delete=req.allow_delete, update=req.allow_update,
                        create=req.allow_create)
    unknown = guard.ambiguous(allow, {n: set() for n in stacks})
    if unknown:
        raise guard.GuardError("refused, nothing was applied:\n  " + "\n  ".join(unknown))
    # Render everything, and resolve every backend, before changing
    # anything: a model that does not evaluate, or a backend secret that
    # does not decrypt, fails the deploy with nothing applied. The program
    # each stack runs is a copy in which the guests this request does not
    # name are protected (guard.protect).
    projects = {n: render.render(s, n, st, ev, rundir, guard.scope(allow, n)) for n, st in stacks.items()}
    envs = {n: backends.env_for(s, n, st["backend"], ev, st) for n, st in stacks.items()}
    # The store path and sha256 of each program: exactly what this deploy ran.
    result["programs"] = {n: p.file for n, p in projects.items()}
    result["program_sha256"] = {n: p.sha256 for n, p in projects.items()}
    # A bare name must mean one resource: refuse a request whose name is a
    # resource of two of its stacks before anything runs.
    names, states = {}, {}
    for n, p in projects.items():
        ev.check()
        st = infra.open_stack(s, p.wd, n, envs[n], ev, install=False)
        states[n] = infra.state(st)
        names[n] = _names(guard.load_program(p.wd), states[n])
    unclear = guard.ambiguous(allow, names)
    if unclear:
        raise guard.GuardError("refused, nothing was applied:\n  " + "\n  ".join(unclear))
    shared = {k for n in names for k in names[n] if sum(k in v for v in names.values()) > 1}
    labels = {n: (lambda key, n=n: f"{n}/{key}" if key in shared else key) for n in stacks}
    # Every stack is previewed, and every plan passes the guard, before
    # any stack is applied: a refusal leaves all of them as they were.
    # (infra.plan reads the stack's state first: a pending operation refuses
    # the run, and before a deploy every guest in state that is not named is
    # protected there. `others`: what the other stacks of the run hold, so
    # the delete of a guest another state entry still manages is refused.)
    planned = {}
    for n, p in projects.items():
        ev.check()
        others = {m: st for m, st in states.items() if m != n}
        planned[n] = infra.plan(s, p.wd, n, envs[n], ev, req.preview, req.refresh, req.targets,
                                guard.scope(allow, n), labels[n], others, stacks[n].get("adoptIds") or {})
        result["infra"][n], result["plan"][n] = planned[n]["changes"], planned[n]["plan"]
        if planned[n]["refused"]:
            result["refused"][n] = planned[n]["refused"]
    if req.preview:
        return
    if result["refused"]:
        raise guard.GuardError(guard.message(result["refused"]), dict(result))
    for n, p in projects.items():
        ev.check()
        # What the directory holds must still be what was rendered and
        # planned; the engine is then bound to the plan (infra.apply).
        p.verify()
        done = infra.apply(s, p.wd, n, envs[n], ev, planned[n], req.refresh, req.targets,
                           guard.scope(allow, n), labels[n], {m: st for m, st in states.items() if m != n},
                           stacks[n].get("adoptIds") or {})
        result["infra"][n] = done["changes"]
        result["applied"].append(n)
        if done.get("unprotected"):
            result.setdefault("unprotected", {})[n] = done["unprotected"]
