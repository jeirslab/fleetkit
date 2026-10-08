"""NixOS stage: Colmena on the estate repo's hive.

The estate repo exposes hives as `hives.<hive>` (lib.mkHive), in the classic
hive shape (meta.nixpkgs is a nixpkgs instance). Colmena reads a hive from a
file, so a one-line hive.nix that loads the flake's hive is written into the
run's directory. Preview builds every node locally and touches no host.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from .events import Emitter
from .settings import Settings

GOALS = ("switch", "test", "boot", "dry-activate")


class NixosError(Exception):
    pass


def hive_file(s: Settings, hive: str, where: Path | None = None) -> str:
    """In the run's own directory (render.Run): another run, at another
    checkout, writes its own."""
    d = (where or s.state_dir / "work") / "_hives"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{hive}.nix"
    f.write_text(f"(builtins.getFlake (toString {s.flake})).hives.{hive}\n")
    return str(f)


def run(s: Settings, hive: str, ev: Emitter, goal: str, preview: bool,
        on: list[str] | None = None, where: Path | None = None) -> None:
    ev.check()
    if goal not in GOALS:
        raise NixosError(f"goal {goal!r} is not one of {', '.join(GOALS)}")
    verb = ["build"] if preview else ["apply", goal]
    cmd = [s.colmena, *verb, "-f", hive_file(s, hive, where), "--impure"]
    if on:
        cmd += ["--on", ",".join(on)]
    ev.emit("nixos", "start", hive=hive, command=" ".join(cmd[1:]))
    p = subprocess.Popen(cmd, cwd=s.flake, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    remove = ev.on_cancel(p.terminate)
    try:
        assert p.stdout is not None
        for line in p.stdout:
            ev.emit("nixos", "log", hive=hive, line=line.rstrip("\n"))
        rc = p.wait()
    finally:
        remove()
        if p.poll() is None:
            t = threading.Timer(10, p.kill)
            t.start()
            p.wait()
            t.cancel()
    ev.check()
    if rc != 0:
        raise NixosError(f"colmena {' '.join(verb)} exited {rc}")
    ev.emit("nixos", "done", hive=hive)
