"""NixOS stage: Colmena on the estate repo's hive.

The estate repo exposes hives as `hives.<hive>` (lib.mkHive), in the classic
hive shape (meta.nixpkgs is a nixpkgs instance). Colmena reads a hive from a
file, so a one-line hive.nix that loads the flake's hive is written into the
run's directory. Preview builds every node locally and touches no host;
`changes` (a preview with `compare`) only reads each node's live system.
"""
from __future__ import annotations

import json
import re
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


# `colmena exec` prints each node's output as "<node> | <line>" (0.4.0, stderr).
_LIVE_RE = re.compile(r"^\s*(\S+) \| (/nix/store/\S+)$")


def changes(s: Settings, hive: str, ev: Emitter, on: list[str], where: Path | None = None,
            timeout: float = 300) -> dict[str, dict]:
    """-> {node: {"built", "live", "tags"}} for each node of `on` the hive has:
    the toplevel this checkout builds and the one the node runs now (None: it
    could not be read). Reads only: colmena eval, then readlink over colmena
    exec. A failed eval raises; an unreachable node is live None."""
    f = hive_file(s, hive, where)
    expr = ("{ nodes, lib, ... }: lib.genAttrs (builtins.filter (n: nodes ? ${n}) (builtins.fromJSON ''%s'')) "
            "(n: { built = nodes.${n}.config.system.build.toplevel.outPath; "
            "tags = nodes.${n}.config.deployment.tags; })" % json.dumps(on))
    p = subprocess.run([s.colmena, "eval", "-f", f, "--impure", "-E", expr], cwd=s.flake,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise NixosError(f"colmena eval of the toplevels exited {p.returncode}: {p.stderr[-500:]}")
    nodes = {n: {**v, "live": None} for n, v in json.loads(p.stdout).items()}
    if nodes:
        try:
            out = subprocess.run([s.colmena, "exec", "-f", f, "--impure", "--on", ",".join(nodes), "--",
                                  "readlink", "-e", "/run/current-system"], cwd=s.flake, capture_output=True,
                                 text=True, timeout=timeout)
            lines = (out.stdout + out.stderr).splitlines()
        except subprocess.TimeoutExpired as e:  # what came back counts; the rest stays unknown
            lines = (((e.stdout or b"") + (e.stderr or b"")).decode(errors="replace")).splitlines()
        seen: dict[str, set[str]] = {}
        for line in lines:
            m = _LIVE_RE.match(line)
            if m and m.group(1) in nodes:
                seen.setdefault(m.group(1), set()).add(m.group(2))
        for n, paths in seen.items():
            if len(paths) == 1:  # two answers from one node is no answer
                nodes[n]["live"] = paths.pop()
    for n, c in nodes.items():
        ev.emit("nixos", "compare", hive=hive, node=n, built=c["built"], live=c["live"])
    return nodes
