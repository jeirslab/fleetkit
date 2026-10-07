"""Render stage: evaluate the estate repo's Pulumi programs with Nix and lay
each out as a Pulumi project directory. This is the only place the model is
read, and it is read as the evaluated output, never interpreted.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .events import Emitter
from .settings import Settings


class RenderError(Exception):
    pass


def nix_eval_json(s: Settings, attr: str, apply: str | None = None) -> object:
    cmd = [s.nix, "eval", "--json", f"{s.flake}#{attr}"]
    if apply:
        cmd += ["--apply", apply]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        err = "\n".join(line for line in p.stderr.splitlines() if "Git tree" not in line)
        raise RenderError(f"nix eval {attr} failed:\n{err.strip()[-4000:]}")
    return json.loads(p.stdout)


def stacks_of(s: Settings, estate: str) -> list[str]:
    names = nix_eval_json(s, "pulumi", "builtins.mapAttrs (_: builtins.attrNames)")
    if estate not in names:
        raise RenderError(f"no estate {estate!r} under pulumi (have: {', '.join(sorted(names))})")
    return sorted(names[estate])


def estates(s: Settings) -> dict[str, list[str]]:
    return nix_eval_json(s, "pulumi", "builtins.mapAttrs (_: builtins.attrNames)")


def _absolute_secrets(program: dict, roots: list[Path]) -> None:
    """The sops provider resolves a relative source_file against its own working
    directory (the project dir here); point each at the file under the first
    secret root that has it."""
    for name, var in (program.get("variables") or {}).items():
        args = (var.get("fn::invoke") or {}).get("arguments") or {}
        src = args.get("sourceFile")
        if not src or Path(src).is_absolute():
            continue
        hit = next((r / src for r in roots if (r / src).is_file()), None)
        if hit is None:
            raise RenderError(
                f"variables.{name}: secrets file {src} is under none of "
                f"{', '.join(map(str, roots))} (FLEETKIT_SECRET_ROOTS adds more)")
        args["sourceFile"] = str(hit)


def render(s: Settings, estate: str, stack: str, ev: Emitter) -> Path:
    ev.emit("render", "start", estate=estate, stack=stack)
    program = nix_eval_json(s, f"pulumi.{estate}.{stack}")
    if not isinstance(program, dict) or program.get("runtime") != "yaml":
        raise RenderError(f"pulumi.{estate}.{stack} is not a Pulumi YAML program")
    _absolute_secrets(program, s.secret_roots)
    wd = s.state_dir / "work" / estate / stack
    wd.mkdir(parents=True, exist_ok=True)
    # JSON is YAML; Pulumi reads it as Pulumi.yaml.
    (wd / "Pulumi.yaml").write_text(json.dumps(program, indent=1, sort_keys=True) + "\n")
    ev.emit("render", "done", estate=estate, stack=stack, project=program["name"],
            resources=len(program.get("resources") or {}), workdir=str(wd))
    return wd
