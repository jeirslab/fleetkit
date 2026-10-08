"""Render stage: evaluate the estate repo's Pulumi.nix stacks and lay each out
as a Pulumi project directory. The repo exposes them as `pulumi.<stack>`
(fleetkit.lib.pulumi.stacks); each carries its estate, its backend and
`file`, the checked program already written to the store by Nix. This is the
only place the model is read, and it is read as evaluated output.

Every run (a deploy, a preview, an adoption, `fleetkit render`) renders into a
directory of its own, which no other run writes:

  <state>/runs/<time>-<pid>-<random>/.lock      held (flock) for the life of the run
  <state>/runs/<time>-<pid>-<random>/<stack>/
    program      ->  /nix/store/<hash>-Pulumi.yaml   (a GC root; what the estate declares)
    Pulumi.yaml       the program the engine runs: a copy of it in which the
                      guests the request did not name are protected (guard.protect)
    secrets/...  ->  the sops files its invokes read (relative to here)
    Pulumi.<stack>.yaml   Pulumi's settings of the stack, copied in from
                          <state>/stacks/<stack>/ (below)
    update-plan.json      the plan the `up` is bound to (infra.py)

It used to be <state>/work/<stack>/, one directory per stack whose Pulumi.yaml
link every render repointed: a deploy then applied whatever the last render
(another job, a preview, an adoption) had left there, not what it previewed.
The run records the sha256 of both files when it renders, and the `up` is
refused if either differs by then (`Project.verify`).

Pulumi's settings file of a stack (`Pulumi.<stack>.yaml`, the passphrase
salt) is the one thing that outlives a run: it is kept in
<state>/stacks/<stack>/ and copied into each run's directory. (Checked with
Pulumi 3.247 and a file backend: without it a new salt is made, the state's
secrets still decrypt, since the state carries its own salt, and the next `up`
re-encrypts the state with the new one. Keeping the file keeps one salt.)
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import guard
from .events import Emitter
from .settings import Settings

# A run directory nobody removed (a kill) is swept once its run is over (its
# lock can be taken) and it is this old. `fleetkit render` keeps its directory
# for that long.
STALE = 24 * 3600
LOCK = ".lock"


class RenderError(Exception):
    pass


# Only what the runner needs of each stack, so the eval serialises no program.
# adoptIds ({ <resource key> = <provider id>; }) and adoptUnresolved
# ({ <resource key> = <why no id>; }) are what `fleetkit adopt` imports by;
# a kit that does not expose them yet adopts only by --id.
VIEW = ("s: builtins.mapAttrs (_: x: { inherit (x) estate project backend file secrets; "
        "adoptIds = x.adoptIds or { }; adoptUnresolved = x.adoptUnresolved or { }; }) s")


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
    out = nix_eval_json(s, "pulumi", VIEW)
    for st in out.values():
        st["adoptIds"] = st.get("adoptIds") or {}
        st["adoptUnresolved"] = st.get("adoptUnresolved") or {}
    return out


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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class Run:
    """The directory of one run. `close()` removes it.

    The run holds an exclusive flock on `.lock` in it from the start to
    `close()` (or to the end of the process, however it ends: the kernel drops
    the lock). That lock, not the pid in the name, is what says the run is
    alive: a pid says nothing about another run of the same `serve` process,
    nor about a run on another host that shares the state directory."""

    def __init__(self, s: Settings, kind: str, keep: bool = False):
        self.kind, self.keep = kind, keep
        root = s.state_dir / "runs"
        root.mkdir(parents=True, exist_ok=True)
        sweep(root)
        self.id = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.dir = root / self.id
        self.dir.mkdir(mode=0o700)
        self._lock: Optional[int] = os.open(self.dir / LOCK, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a new file in a new directory: free

    def close(self) -> None:
        if not self.keep:
            shutil.rmtree(self.dir, ignore_errors=True)  # while the lock is still held
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def __enter__(self) -> "Run":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def sweep(root: Path) -> list[str]:
    """Remove run directories that are STALE old and whose run is over: its
    lock can be taken. The lock is held while the directory is removed, so no
    two sweeps take the same one. A directory without a lock file is of a
    runner from before the lock: for those, as then, the pid in the name."""
    gone = []
    for d in root.iterdir() if root.is_dir() else []:
        parts = d.name.split("-")
        try:
            pid, age = int(parts[1]), time.time() - d.stat().st_mtime
        except (IndexError, ValueError, OSError):
            continue
        if age <= STALE:
            continue
        try:
            fd = os.open(d / LOCK, os.O_RDWR | os.O_CLOEXEC)
        except FileNotFoundError:
            if pid != os.getpid() and _alive(pid):
                continue
            fd = None
        except OSError:
            continue
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue  # held: the run is alive, here or elsewhere
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name)
        finally:
            if fd is not None:
                os.close(fd)
    return gone


@dataclass
class Project:
    """A stack rendered for one run: where, and what exactly."""
    stack: str
    wd: Path
    file: str        # the store path of the estate's program
    sha256: str      # of that file
    run_sha256: str  # of wd/Pulumi.yaml, the program the engine runs
    protected: list[str] = field(default_factory=list)  # guests the run protected

    def verify(self) -> None:
        """Refuse (GuardError) unless the directory still holds what was
        rendered: called before every `up`."""
        why = []
        try:
            if guard.sha256(self.wd / "Pulumi.yaml") != self.run_sha256:
                why.append(f"{self.wd / 'Pulumi.yaml'} is not the program that was planned")
            if guard.sha256(self.wd / "program") != self.sha256:
                why.append(f"{self.wd / 'program'} no longer is {self.file} as planned")
        except OSError as e:
            why.append(f"the rendered program cannot be read: {e}")
        if why:
            raise guard.GuardError(
                f"refused, nothing was applied: {self.stack}: the program changed between the plan and the "
                f"up ({'; '.join(why)}; planned: {self.file} sha256 {self.sha256})")


def settings_file(s: Settings, name: str) -> Path:
    """Where Pulumi's settings of a stack are kept between runs."""
    return s.state_dir / "stacks" / name / f"Pulumi.{s.stack}.yaml"


def settings_in(s: Settings, name: str, wd: Path) -> None:
    """Copy the kept settings file into a run's project dir."""
    kept = settings_file(s, name)
    if not kept.is_file():
        legacy = s.state_dir / "work" / name / kept.name  # where it lived before per-run dirs
        if not legacy.is_file():
            return
        _keep(legacy, kept)
    shutil.copyfile(kept, wd / kept.name)


def settings_out(s: Settings, name: str, wd: Path) -> None:
    """Keep the settings file Pulumi made on the stack's first run. The first
    one stays: a later run never overwrites it."""
    made, kept = wd / f"Pulumi.{s.stack}.yaml", settings_file(s, name)
    if made.is_file() and not kept.is_file():
        _keep(made, kept)


def _keep(src: Path, kept: Path) -> None:
    kept.parent.mkdir(parents=True, exist_ok=True)
    tmp = kept.with_name(f".{kept.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    shutil.copyfile(src, tmp)
    try:
        os.link(tmp, kept)  # atomic, and fails if another run kept one first
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)


def render(s: Settings, name: str, st: dict[str, Any], ev: Emitter, run: Run,
           allow: Optional[guard.Allow] = None) -> Project:
    """Lay the stack out in the run's directory. `allow` (scoped to the stack)
    names the guests that are NOT protected in the program the engine runs."""
    ev.check()
    ev.emit("render", "start", stack=name)
    wd = run.dir / name
    wd.mkdir(mode=0o700)
    _root(s, wd / "program", st["file"])
    raw = (wd / "program").read_bytes()
    program, protected = guard.protect(s.stack, json.loads(raw), allow or guard.Allow())
    text = json.dumps(program)
    (wd / "Pulumi.yaml").write_text(text)
    link_secrets(s, wd, st)
    settings_in(s, name, wd)
    ev.emit("render", "done", stack=name, project=st["project"], program=st["file"], workdir=str(wd),
            protected=protected)
    return Project(stack=name, wd=wd, file=st["file"], sha256=hashlib.sha256(raw).hexdigest(),
                   run_sha256=hashlib.sha256(text.encode()).hexdigest(), protected=protected)


def link_secrets(s: Settings, wd: Path, st: dict[str, Any]) -> None:
    """The sops files a program's invokes read, linked where it looks for them."""
    for src in st["secrets"]:
        if Path(src).is_absolute():
            continue
        link = wd / src
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(find_secret(s, src))
