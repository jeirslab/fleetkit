"""One deploy: render every stack, provision with Pulumi, configure with
Colmena. The CLI and the API run exactly this.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from . import backends, infra, nixos, render
from .events import Emitter
from .settings import Settings


class DeployRequest(BaseModel):
    estate: str = Field(description="Estate name: the programs under pulumi.<estate>.")
    stacks: Optional[list[str]] = Field(
        default=None, description="Pulumi stacks to run (default: every stack of the estate).")
    rev: Optional[str] = Field(
        default=None, description="Git commit or branch to deploy (server with a repo only; default the branch head).")
    infra: bool = Field(default=True, description="Run the Pulumi stage.")
    nixos: bool = Field(default=True, description="Run the Colmena stage.")
    hive: Optional[str] = Field(default=None, description="Hive name under hives.* (default: the estate).")
    on: list[str] = Field(default_factory=list, description="Colmena --on: node names or @tags.")
    goal: Literal["switch", "test", "boot", "dry-activate"] = "switch"
    preview: bool = Field(default=False, description="pulumi preview and colmena build; nothing changes.")
    refresh: bool = Field(default=False, description="Refresh Pulumi state from the providers first.")
    targets: list[str] = Field(default_factory=list, description="Pulumi --target URNs.")


def run(s: Settings, req: DeployRequest, ev: Emitter) -> dict[str, Any]:
    result: dict[str, Any] = {"infra": {}, "programs": {}, "nixos": None}
    if req.infra:
        stacks = render.stacks_of(s, req.estate, req.stacks)
        # Render everything, and resolve every backend, before changing
        # anything: a model that does not evaluate, or a backend secret that
        # does not decrypt, fails the deploy with nothing applied.
        workdirs = {n: render.render(s, n, st, ev) for n, st in stacks.items()}
        envs = {n: backends.env_for(s, n, st["backend"], ev) for n, st in stacks.items()}
        # The store path of each program: exactly what this deploy ran.
        result["programs"] = {n: st["file"] for n, st in stacks.items()}
        for n in stacks:
            ev.check()
            result["infra"][n] = infra.run(s, workdirs[n], n, envs[n], ev, req.preview,
                                           req.refresh, req.targets)
    if req.nixos:
        # After infra: the hive is evaluated now, against what was provisioned.
        nixos.run(s, req.hive or req.estate, ev, req.goal, req.preview, req.on)
        result["nixos"] = "built" if req.preview else req.goal
    return result
