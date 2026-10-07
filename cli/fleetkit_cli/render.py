"""Render stage: evaluate the estate repo's Pulumi.nix stacks and lay each out
as a Pulumi project directory. The repo exposes them as `pulumi.<stack>`
(fleetkit.lib.pulumi.stacks); each carries its estate, its backend and
`file`, the checked program already written to the store by Nix. This is the
only place the model is read, and it is read as evaluated output.

  <state>/work/<stack>/
    Pulumi.yaml  ->  /nix/store/<hash>-Pulumi.yaml   (a GC root; the program, JSON)
    secrets/...  ->  the sops files its invokes read (relative to here)
    Pulumi.<stack>.yaml                               (Pulumi's own, on stack init)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .events import Emitter
from .settings import Settings


class RenderError(Exception):
    pass


# Only what the runner needs of each stack, so the eval serialises no program.
VIEW = "s: builtins.mapAttrs (_: x: { inherit (x) estate project backend file secrets; }) s"


def nix_eval_json(s: Settings, attr: str, apply: str | None = None) -> Any:
    cmd = [s.nix, "eval", "--json", f"{s.flake}#{attr}"]
    if apply:
        cmd += ["--apply", apply]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        err = "\n".join(line for line in p.stderr.splitlines() if "Git tree" not in line)
        raise RenderError(f"nix eval {attr} failed:\n{err.strip()[-4000:]}")
    return json.loads(p.stdout)


def stacks(s: Settings) -> dict[str, dict[str, Any]]:
    return nix_eval_json(s, "pulumi", VIEW)


def estates(s: Settings) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, st in stacks(s).items():
        out.setdefault(st["estate"] or "-", []).append(name)
    return {e: sorted(v) for e, v in sorted(out.items())}


def stacks_of(s: Settings, estate: str, only: list[str] | None = None) -> dict[str, dict[str, Any]]:
    all_ = stacks(s)
    mine = {n: st for n, st in all_.items() if st["estate"] == estate}
    if not mine:
        have = ", ".join(sorted({st["estate"] or "-" for st in all_.values()}))
        raise RenderError(f"no stack of estate {estate!r} under pulumi (estates: {have})")
    if only:
        unknown = [n for n in only if n not in mine]
        if unknown:
            raise RenderError(f"estate {estate} has no stack {', '.join(unknown)} (has: {', '.join(sorted(mine))})")
        mine = {n: mine[n] for n in only}
    return mine


def find_secret(s: Settings, src: str) -> Path:
    if Path(src).is_absolute():
        return Path(src)
    hit = next((r / src for r in s.secret_roots if (r / src).is_file()), None)
    if hit is None:
        raise RenderError(f"secrets file {src} is under none of {', '.join(map(str, s.secret_roots))} "
                          f"(FLEETKIT_SECRET_ROOTS adds more)")
    return hit


def _root(s: Settings, link: Path, target: str) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    # nix build of a store path only links it, and makes the link a GC root:
    # a collection during a long deploy cannot take the program away.
    p = subprocess.run([s.nix, "build", "--out-link", str(link), target], capture_output=True, text=True)
    if p.returncode != 0:
        raise RenderError(f"cannot root {target}: {p.stderr.strip()[-2000:]}")


def render(s: Settings, name: str, st: dict[str, Any], ev: Emitter) -> Path:
    ev.emit("render", "start", stack=name)
    wd = s.state_dir / "work" / name
    wd.mkdir(parents=True, exist_ok=True)
    for stale in ("Pulumi.json", "Main.json", "Main.yaml"):
        (wd / stale).unlink(missing_ok=True)
    _root(s, wd / "Pulumi.yaml", st["file"])
    for src in st["secrets"]:
        if Path(src).is_absolute():
            continue
        link = wd / src
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(find_secret(s, src))
    ev.emit("render", "done", stack=name, project=st["project"], program=st["file"], workdir=str(wd))
    return wd
