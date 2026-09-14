"""fleet sessions — native-process job runner for long-running fleet ops.

Any command that might run for more than ~30s (Colmena deploys, `tofu
apply`, fleet-wide NixOS rollouts) is run as a **detached native
subprocess** — NOT a tmux session (jeirslab/fleetkit#7). tmux was standing
in for three things at once; each is now a plain file so the mechanism works
headless (in CI and the CD runner) and, crucially, tells the truth:

  * process supervisor → a detached `fleet _run-job` child that runs the
    command, streams its output to a log, and records the exit code on exit;
  * log sink          → ``<name>.log`` (follow with `fleet sessions logs`);
  * status registry   → ``<name>.status`` JSON ({status, pid, exit_code, …}).

Because the supervisor records the real exit code, a caller that WAITS gets
the authoritative result instead of "launched" — `dispatch` still returns
immediately for fire-and-forget, but `run_and_wait` blocks and aggregates
the real exit codes (what `fleet deploy … --wait` and the CD runner use).

Session naming convention: ``fleet-<family>-<target>`` (e.g.
``fleet-deploy-netgate``). Opt out of backgrounding with ``--no-session`` or
``FLEET_NO_SESSION=1`` (CI / the runner's inner invocation).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ._util import env_get

console = Console()

# ── State persistence ────────────────────────────────────────────────
# One JSON record + one log file per job. User-global (XDG), NOT the
# project-root `.cache/fleet` that _util.fleet_cache_dir() manages.
_CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
_STATE_DIR = _CACHE_HOME / "fleet" / "sessions"
_LEGACY_STATE_DIR = _CACHE_HOME / "sk" / "sessions"

SESSION_PREFIX = "fleet-"
LEGACY_SESSION_PREFIX = "sk-"


def _ensure_state_dir() -> None:
    # One-time silent migration of the pre-INFRA-218 location.
    if not _STATE_DIR.exists() and _LEGACY_STATE_DIR.is_dir():
        try:
            _STATE_DIR.parent.mkdir(parents=True, exist_ok=True)
            os.rename(_LEGACY_STATE_DIR, _STATE_DIR)
        except OSError:
            pass
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _state_file(name: str) -> Path:
    return _STATE_DIR / f"{name}.status"


def _log_file(name: str) -> Path:
    return _STATE_DIR / f"{name}.log"


def _read_record(name: str) -> dict | None:
    f = _state_file(name)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _write_record(name: str, **fields) -> None:
    """Merge ``fields`` into the job's record (create if absent)."""
    _ensure_state_dir()
    rec = _read_record(name) or {"name": name}
    rec.update(fields)
    rec["updated_at"] = time.time()
    _state_file(name).write_text(json.dumps(rec))


# ── Liveness (replaces tmux has-session / pane-dead) ─────────────────

def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _job_state(name: str) -> tuple[str, int | None]:
    """Authoritative (state, exit_code) for a job.

    A RUN record whose supervisor pid is gone is a crash the supervisor
    never got to record (SIGKILL, OOM, power loss) — report STALE rather
    than a stuck RUN, so status never lies about a job that is not running.
    """
    rec = _read_record(name)
    if not rec:
        return ("UNKNOWN", None)
    status = rec.get("status", "UNKNOWN")
    if status == "RUN" and not _pid_alive(rec.get("pid")):
        return ("STALE", None)
    return (status, rec.get("exit_code"))


def _last_log_line(name: str, limit: int = 120) -> str:
    f = _log_file(name)
    if not f.exists():
        return ""
    try:
        tail = f.read_bytes()[-8192:].decode("utf-8", "replace")
    except OSError:
        return ""
    for raw in reversed(tail.splitlines()):
        s = raw.strip()
        if s:
            return s[:limit]
    return ""


def running_inside(session_name: str) -> bool:
    return env_get("FLEET_SESSION_NAME") == session_name


# ── The job primitive ────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def run_job(name: str, cmd: list[str], *, cwd: Path | None = None,
            description: str | None = None, tee: bool = False) -> int:
    """Run ``cmd`` to completion IN THIS PROCESS, recording the job.

    Streams combined stdout/stderr to the job's log (optionally teeing to
    this process's stdout), records RUN→DONE-OK/DONE-FAIL with the real exit
    code, and returns it. This is the single execution path shared by the
    detached supervisor (`_run-job`) and the synchronous `run_and_wait`.
    """
    _ensure_state_dir()
    log_path = _log_file(name)
    _write_record(name, status="RUN", pid=os.getpid(), exit_code=None,
                  description=description or " ".join(cmd), log=str(log_path),
                  cmd=cmd, started_at=_now(), finished_at=None)
    rc = 1
    try:
        with open(log_path, "wb") as lf:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd) if cwd else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.readline(), b""):
                lf.write(chunk)
                lf.flush()
                if tee:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            rc = proc.wait()
    except FileNotFoundError as exc:
        with open(log_path, "ab") as lf:
            lf.write(f"fleet: {exc}\n".encode())
        rc = 127
    finally:
        _write_record(name, status=("DONE-OK" if rc == 0 else "DONE-FAIL"),
                       exit_code=rc, finished_at=_now())
    return rc


def dispatch_session(
    session_name: str,
    cmd: list[str],
    *,
    cwd: Path | None = None,
    description: str | None = None,
    env: dict | None = None,
) -> int:
    """Launch ``cmd`` as a DETACHED native job (fire-and-forget).

    - Returns -1 if backgrounding is opted out (FLEET_NO_SESSION) or we are
      already the inner job (FLEET_SESSION_NAME) — the caller runs inline.
    - Returns 2 if a job of this name is already running (double-run guard).
    - Otherwise spawns a detached `fleet _run-job` supervisor, waits briefly
      to catch an immediate death, and returns 0 on a clean launch.
    """
    from ._util import fleet_executable

    if env_get("FLEET_NO_SESSION") == "1":
        return -1
    if running_inside(session_name):
        return -1

    state, _ = _job_state(session_name)
    if state == "RUN":
        console.print(
            f"[red]ERROR:[/red] job [bold]{session_name}[/bold] is already running.\n"
            f"  logs:  fleet sessions logs {session_name} -f\n"
            f"  kill:  fleet sessions kill {session_name}"
        )
        return 2

    _ensure_state_dir()
    supervisor = [
        fleet_executable(), "_run-job",
        "--name", session_name,
        "--desc", description or " ".join(cmd),
    ]
    if cwd:
        supervisor += ["--cwd", str(cwd)]
    supervisor += ["--", *cmd]

    child_env = dict(os.environ)
    child_env["FLEET_SESSION_NAME"] = session_name
    if env:
        child_env.update({k: str(v) for k, v in env.items()})

    # start_new_session detaches from this process group so the job outlives
    # the terminal; output goes to the log, not our fds.
    proc = subprocess.Popen(
        supervisor, cwd=str(cwd) if cwd else None, env=child_env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    _write_record(session_name, status="RUN", pid=proc.pid, exit_code=None,
                  description=description or " ".join(cmd),
                  log=str(_log_file(session_name)), started_at=_now())

    # Early-death watch (INFRA-171): a job that dies in the first few seconds
    # is almost always an env failure (rc=127, bad cwd). Surface it instead of
    # reporting a launch that never ran.
    deadline = time.time() + 4.0
    while time.time() < deadline:
        time.sleep(0.25)
        state, exit_code = _job_state(session_name)
        if state == "DONE-OK":
            break
        if state in ("DONE-FAIL", "STALE"):
            rc = int(exit_code) if str(exit_code).isdigit() else 1
            console.print(
                f"[red]ERROR:[/red] job [bold]{session_name}[/bold] died "
                f"immediately (rc={rc}). Last output:"
            )
            for line in _log_file(session_name).read_text(errors="replace").splitlines()[-15:]:
                if line.strip():
                    console.print(f"  [dim]{line}[/dim]")
            return rc or 1

    console.print(
        f"[green]launched[/green] {session_name}  "
        f"[dim](logs: fleet sessions logs {session_name} -f · "
        f"status: fleet sessions status · kill: fleet sessions kill {session_name})[/dim]"
    )
    return 0


def run_and_wait(specs: list[dict], *, max_workers: int | None = None) -> int:
    """Run jobs to completion and return the aggregate exit code.

    ``specs`` is a list of ``{name, cmd, cwd?, description?}``. Jobs run
    concurrently (bounded by ``max_workers``; None = one per spec) and their
    real exit codes are collected — the honest path `fleet deploy … --wait`
    and the CD runner use. A single job tees to the terminal; multiple jobs
    stream only to their logs (interleaving many is unreadable) and a summary
    table is printed at the end.
    """
    if not specs:
        return 0
    tee = len(specs) == 1

    def _one(spec: dict) -> tuple[str, int]:
        rc = run_job(spec["name"], spec["cmd"], cwd=spec.get("cwd"),
                     description=spec.get("description"), tee=tee)
        return (spec["name"], rc)

    workers = max_workers or len(specs)
    results: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for name, rc in pool.map(_one, specs):
            results[name] = rc

    if not tee:
        t = Table(title="job results")
        t.add_column("job", style="cyan")
        t.add_column("result", style="bold")
        for name in sorted(results):
            rc = results[name]
            style = "green" if rc == 0 else "red"
            t.add_row(name, f"[{style}]{'ok' if rc == 0 else f'FAIL (rc={rc})'}[/{style}]")
        console.print(t)

    return max(results.values()) if results else 0


# ── Public CLI (fleet sessions …) ───────────────────────────────────────

@dataclass
class _JobInfo:
    name: str
    state: str  # RUN | DONE-OK | DONE-FAIL | STALE | UNKNOWN
    exit_code: int | None
    last_line: str


def _matches_prefix(name: str, prefix: str) -> bool:
    if name.startswith(prefix):
        return True
    return prefix == SESSION_PREFIX and name.startswith(LEGACY_SESSION_PREFIX)


def _collect(prefix: str = SESSION_PREFIX) -> list[_JobInfo]:
    if not _STATE_DIR.is_dir():
        return []
    infos: list[_JobInfo] = []
    for f in sorted(_STATE_DIR.glob("*.status")):
        name = f.stem
        if not _matches_prefix(name, prefix):
            continue
        state, exit_code = _job_state(name)
        infos.append(_JobInfo(name, state, exit_code, _last_log_line(name)))
    return infos


@click.group("sessions")
def sessions_cli() -> None:
    """Manage fleet background jobs (native processes; formerly tmux)."""


@sessions_cli.command("list")
@click.option("--prefix", default=SESSION_PREFIX, help="Job name prefix filter.")
def sessions_list(prefix: str) -> None:
    """One-row-per-job overview (state, exit code, last log line)."""
    infos = _collect(prefix)
    if not infos:
        console.print(f"No '{prefix}*' jobs.")
        return
    t = Table()
    t.add_column("State", style="bold")
    t.add_column("Name", style="cyan")
    t.add_column("rc")
    t.add_column("Last output", style="dim")
    for i in sorted(infos, key=lambda x: x.name):
        style = {"RUN": "yellow", "DONE-OK": "green",
                 "DONE-FAIL": "red", "STALE": "red"}.get(i.state, "white")
        rc = "" if i.exit_code is None else str(i.exit_code)
        t.add_row(f"[{style}]{i.state}[/{style}]", i.name, rc, i.last_line)
    console.print(t)


@sessions_cli.command("status")
@click.option("--prefix", default=SESSION_PREFIX)
def sessions_status(prefix: str) -> None:
    """Alias of list."""
    ctx = click.get_current_context()
    ctx.invoke(sessions_list, prefix=prefix)


@sessions_cli.command("logs")
@click.argument("name")
@click.option("-f", "--follow", is_flag=True, help="Follow the log (tail -f).")
def sessions_logs(name: str, follow: bool) -> None:
    """Print (or follow) a job's log — the native replacement for `attach`."""
    log = _log_file(name)
    if not log.exists():
        console.print(f"[red]ERROR:[/red] no log for {name}")
        sys.exit(1)
    if follow:
        os.execvp("tail", ["tail", "-n", "+1", "-f", str(log)])
    sys.stdout.write(log.read_text(errors="replace"))


@sessions_cli.command("attach")
@click.argument("name")
def sessions_attach(name: str) -> None:
    """Follow a running job's log (kept for muscle memory; see `logs`)."""
    ctx = click.get_current_context()
    ctx.invoke(sessions_logs, name=name, follow=True)


@sessions_cli.command("kill")
@click.argument("name")
def sessions_kill(name: str) -> None:
    """Kill a specific job, or 'all' for every fleet-* job."""
    targets = [i.name for i in _collect(SESSION_PREFIX)] if name == "all" else [name]
    for tgt in targets:
        rec = _read_record(tgt)
        pid = rec.get("pid") if rec else None
        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except (OSError, ValueError):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError):
                    pass
            _write_record(tgt, status="DONE-FAIL", exit_code=143, finished_at=_now())
            console.print(f"killed {tgt}")
        else:
            console.print(f"[yellow]{tgt} not running[/yellow]")


# ── Detached supervisor (internal) ───────────────────────────────────

@click.command("_run-job", hidden=True)
@click.option("--name", required=True)
@click.option("--desc", default=None)
@click.option("--cwd", default=None, type=click.Path())
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
def run_job_cli(name: str, desc: str | None, cwd: str | None, cmd: tuple[str, ...]) -> None:
    """INTERNAL: run one job to completion, recording its status/log.

    Spawned detached by `dispatch_session`; not for direct use. Everything
    after ``--`` is the command to run.
    """
    argv = list(cmd)
    if argv and argv[0] == "--":
        argv = argv[1:]
    rc = run_job(name, argv, cwd=Path(cwd) if cwd else None, description=desc, tee=False)
    sys.exit(rc)
