"""Infra stage: run a rendered Pulumi project through the Automation API.

The Automation API drives the pulumi CLI underneath, but gives structured
engine events (per resource step, diagnostics, the summary), which is what an
API caller needs to follow a deploy. Secrets are never shown: up() defaults to
show_secrets=True, so it is passed explicitly.

Every `up` is two calls: plan() previews the stack, builds the per-resource
change list the guard reads (guard.py) and saves the engine's own update plan
(`pulumi preview --save-plan`); apply() runs `pulumi up --plan` on that file,
so the engine itself refuses a step the preview did not have, whatever changed
in between (the program, the state). There is no `up` without a plan.

Stopping. `pulumi cancel` is never called: on a self-managed backend it only
deletes the stack's lock file and returns, while the engine runs on, now
unlocked (seen with Pulumi 3.247). The pulumi process is started here
(OwnedPulumi), in its own session, and is stopped by signalling it: SIGINT
once makes the engine finish the step in flight, start no other, save the
state and release its own lock (seen). A second SIGINT asks it to terminate
at once; with a provider call in flight it does not (seen), so every process
of its session is killed shortly after: the engine and its plugins. (Pulumi
starts each plugin in a process group of its own, inside the engine's session,
so killing the engine's process group left the provider plugins running, and
with them the provider call in flight: seen.) That leaves the lock and a
pending operation in state. The lock is never touched here. What was done by
then is reported from the step events (`stopped`).

Pending operations. A run that was killed, or lost its backend, in the middle
of a step leaves that step in the state's `pending_operations`. Pulumi warns
and goes on: after an interrupted create the next `up` creates the resource
again (seen: a second guest), and the entry stays until someone removes it.
plan() reads the state first and refuses while there is one (PENDING_HOW).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pulumi import automation as auto
from pulumi.automation import events as _events
from pulumi.automation._cmd import CommandResult, PulumiCommand, _fixup_path
from pulumi.automation.errors import create_command_error

from . import guard, render
from .events import Cancelled, Emitter
from .settings import Settings

# (the resource's plan entry, the step's op) -> why this step must not run, or
# None. Called before each step of an `up`.
Tripwire = Callable[[dict[str, Any], str], Optional[str]]

# After the first SIGINT the engine waits for the step in flight, however long
# that takes (a clone can take minutes), and nothing here cuts it short unless
# FLEETKIT_STOP_GRACE (seconds) says so: the caller insists by cancelling
# again. After the second SIGINT the engine says "terminating" but, with a
# provider call in flight, does not exit (seen: 100 s and counting with the
# tls provider), so its session is killed HARD_GRACE seconds later.
GRACE = float(os.environ.get("FLEETKIT_STOP_GRACE", "0"))
HARD_GRACE = 10.0
# How long the kill waits for the processes of the session to be gone.
KILL_WAIT = 3.0

# What to do about a pending operation: said by plan()'s refusal and by a run
# that stopped with a step in flight.
PENDING_HOW = (
    "For each, look at what is really there: does the resource exist (for a guest: is there a container or VM "
    "with its vmid on the node)? If it exists, adopt it first (`fleetkit adopt <estate> --resource <key> "
    "[--id <key>=<node>/<vmid>] --apply`), then remove the pending operation; if it does not exist, remove the "
    "pending operation. To remove it: `pulumi stack export --file state.json`, delete the entry from "
    "`deployment.pending_operations`, `pulumi stack import --file state.json` (with the stack's backend and "
    "passphrase). Until then every preview and deploy of the stack is refused: a deploy would run the operation "
    "again, and a second create makes a second guest")

PLAN_FILE = "update-plan.json"


class EngineError(RuntimeError):
    """A preview or an up that failed. The text is the engine's, without the
    property values of a plan violation (they can be secret)."""


# The pinned SDK (3.192) reads the engine's `detailedDiff` under the name
# `detailed_diff`, which the engine never writes, so StepEventMetadata.
# detailed_diff is always None. Read it here under its real name. A no-op once
# the SDK is fixed.
_step_from_json = _events.StepEventMetadata.from_json.__func__


def _from_json(cls: Any, data: dict) -> Any:
    m = _step_from_json(cls, data)
    if m.detailed_diff is None and data.get("detailedDiff"):
        m.detailed_diff = data["detailedDiff"]
    return m


_events.StepEventMetadata.from_json = classmethod(_from_json)  # type: ignore[method-assign]


def scrub_violation(text: str) -> str:
    """A plan violation names each changed property with its values
    (`=~keepers[{map[p:{...}]}]`), secret ones in plain text (seen with a
    RandomPassword result). Keep the property names only."""
    def names(m: re.Match) -> str:
        props = re.findall(r"([~+\-=]{1,2})([A-Za-z0-9_.\-]+)\[", m.group(2))
        return m.group(1) + (", ".join(f"{sign}{name}" for sign, name in props) or "(see a preview)") \
            + " [values withheld]"
    return re.sub(r"(violates plan: properties changed: )([^\n]*)", names, text)


def session_processes(sid: int) -> Optional[dict[int, str]]:
    """pid -> name of every live process whose session is `sid` (zombies are
    not alive), read from /proc; None where there is no /proc to read."""
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    out: dict[int, str] = {}
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            continue  # gone meanwhile
        # "pid (comm) state ppid pgrp session ...": comm may hold spaces and parentheses.
        name = stat[stat.find("(") + 1:stat.rfind(")")]
        rest = stat[stat.rfind(")") + 1:].split()
        if len(rest) > 3 and rest[0] != "Z" and rest[3] == str(sid):
            out[int(pid)] = name
    return out


def kill_session(sid: int, wait: float | None = None) -> tuple[set[int], Optional[dict[int, str]]]:
    """SIGKILL every process of the session `sid`, again until none is left or
    `wait` seconds passed (a process can fork while it is being listed).
    -> (the pids that were signalled, what is still alive: {} when nothing is,
    None when the session could not be listed)."""
    end = time.time() + (KILL_WAIT if wait is None else wait)
    killed: set[int] = set()
    while True:
        alive = session_processes(sid)
        if not alive:
            return killed, alive
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except (ProcessLookupError, PermissionError):
                pass
        if time.time() >= end:
            return killed, session_processes(sid)
        time.sleep(0.05)


class OwnedPulumi(PulumiCommand):
    """The pulumi CLI, each process started and held here so it can be
    signalled. `PulumiCommand.run` of the SDK keeps its Popen to itself."""

    def __init__(self) -> None:
        super().__init__(skip_version_check=os.getenv("PULUMI_AUTOMATION_API_SKIP_VERSION_CHECK") is not None)
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self.signals: list[str] = []   # what stop() sent, in order
        self.outcome: Optional[str] = None  # what the engine was told, and whether it had to be killed
        self._watch: Optional[threading.Thread] = None

    def run(self, args: list[str], cwd: str, additional_env: Any, on_output: Any = None,
            on_error: Any = None) -> CommandResult:
        if "--non-interactive" not in args:
            args.append("--non-interactive")
        env = {**os.environ, **additional_env, "PULUMI_AUTOMATION_API": "true"}
        if os.path.isabs(self.command):
            env = _fixup_path(env, os.path.dirname(self.command))
        out: list[str] = []
        err: list[str] = []

        def consume(stream: Any, callback: Any, chunks: list[str]) -> None:
            for line in iter(stream.readline, ""):
                line = line.rstrip()
                if callback:
                    callback(line)
                chunks.append(line)
            stream.close()

        # Its own session: a Ctrl-C at the terminal reaches fleetkit, which
        # decides what the engine is told (main.py), not the engine directly.
        with subprocess.Popen([self.command, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                              env=env, encoding="utf-8", start_new_session=True) as proc:
            with self._lock:
                self._procs.append(proc)
            try:
                threads = [threading.Thread(target=consume, args=(proc.stdout, on_output, out)),
                           threading.Thread(target=consume, args=(proc.stderr, on_error, err))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                code = proc.wait()
            finally:
                with self._lock:
                    self._procs.remove(proc)
        result = CommandResult(stdout="\n".join(out), stderr="\n".join(err), code=code)
        if code != 0:
            raise create_command_error(result)
        return result

    def running(self) -> list[subprocess.Popen]:
        with self._lock:
            return [p for p in self._procs if p.poll() is None]

    def _send(self, procs: list[subprocess.Popen], sig: signal.Signals) -> None:
        for proc in procs:
            if proc.poll() is not None:
                continue
            try:
                proc.send_signal(sig)
                self.signals.append(sig.name)
            except (ProcessLookupError, PermissionError):
                pass

    def _kill(self, procs: list[subprocess.Popen]) -> tuple[int, list[str]]:
        """SIGKILL the session of each process: the engine and every plugin it
        started. Each was started as the leader of a session of its own (run),
        so the session id is its pid, and it stays the session of its plugins
        after the engine is gone. Not `os.killpg`: Pulumi gives each plugin a
        process group of its own. -> (how many processes were killed, what
        could not be: one line each)."""
        count, left = 0, []
        for proc in procs:
            killed, alive = kill_session(proc.pid)
            if alive is None:
                # No /proc: the engine and its own process group, and it is said.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    killed = {proc.pid}
                except (ProcessLookupError, PermissionError):
                    pass
                left.append(f"the plugins of pulumi process {proc.pid} could not be listed (no /proc)")
            else:
                left += [f"pid {pid} ({name})" for pid, name in sorted(alive.items())]
            count += len(killed)
            if killed:
                self.signals.append(signal.SIGKILL.name)
        return count, left

    def stop(self, hard: bool = False, grace: float | None = None) -> None:
        """Stop the engine. Soft: one SIGINT; the engine finishes the step in
        flight, starts no other, saves the state, releases its lock and exits.
        Hard: a second SIGINT ("terminating"), and SIGKILL of every process
        of its session if it is still there HARD_GRACE seconds later. A soft stop turns
        hard by itself only if `grace` (FLEETKIT_STOP_GRACE) is set. Never
        touches the lock."""
        procs = self.running()
        if not procs:
            return
        first = not self.signals
        self._send(procs, signal.SIGINT)
        self.outcome = ("told to terminate at once" if hard or not first
                        else "told to stop after the step in flight")
        if first or hard:
            self._watch = threading.Thread(target=self._watchdog, daemon=True,
                                           args=(procs, hard and first, hard or not first,
                                                 GRACE if grace is None else grace))
            self._watch.start()

    def _watchdog(self, procs: list[subprocess.Popen], second: bool, hard: bool, grace: float) -> None:
        def gone(seconds: float) -> bool:
            end = time.time() + seconds
            while time.time() < end:
                if all(p.poll() is not None for p in procs):
                    return True
                time.sleep(0.05)
            return all(p.poll() is not None for p in procs)

        if second:
            # Two SIGINTs sent at once can reach the engine as one.
            if gone(0.3):
                return
            self._send(procs, signal.SIGINT)
        if not hard:
            if grace <= 0 or gone(grace):
                return
            self._send(procs, signal.SIGINT)
            self.outcome = f"told to terminate at once: it had not stopped {grace:.0f}s after the first signal"
        if gone(HARD_GRACE):
            # It left by itself. A plugin it did not take along would go on
            # with the provider call it was in: there must be none.
            count, left = self._kill(procs)
            if count or left:
                self.outcome += (f"; it exited and left {count} process(es) of its session behind, which were "
                                 f"killed" + self._survivors(left))
            return
        count, left = self._kill(procs)
        self.outcome = (f"killed, with the plugins of its session ({count} process(es)): it had not terminated "
                        f"{HARD_GRACE:.0f}s after the second signal. The lock of the stack is left as it is "
                        f"(stale: check that no pulumi process runs, then `pulumi cancel` removes it), and the "
                        f"state may hold a pending operation for the step in flight" + self._survivors(left))

    @staticmethod
    def _survivors(left: list[str]) -> str:
        if not left:
            return ""
        return (f". STILL RUNNING after the kill: {'; '.join(left)}. A provider plugin that is alive may still be "
                f"carrying out the call it was in: kill it by hand before anything else is done with this stack")


def _stack(s: Settings, wd: Path, env: dict[str, str]) -> auto.Stack:
    opts = auto.LocalWorkspaceOptions(
        work_dir=str(wd),
        env_vars=env,
        secrets_provider="passphrase",
        pulumi_home=str(s.pulumi_home) if s.pulumi_home else None,
        pulumi_command=OwnedPulumi(),
    )
    return auto.create_or_select_stack(stack_name=s.stack, work_dir=str(wd), opts=opts)


def open_stack(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, install: bool = True) -> Any:
    ev.check()
    st = _stack(s, wd, env)
    # The settings file Pulumi made if the stack is new: kept for later runs.
    render.settings_out(s, stack, wd)
    if install:
        ev.emit("infra", "install", stack=stack)
        st.workspace.install()
    return st


def exported(st: Any) -> dict[str, Any]:
    """The stack's state (the Automation API's export): {resources, pending_operations, ...}."""
    return st.export_stack().deployment or {}


def state(st: Any) -> list[dict[str, Any]]:
    """The resources of the stack's state."""
    return list(exported(st).get("resources") or [])


def pending(deployment: dict[str, Any]) -> list[dict[str, str]]:
    """[{type, urn}] of the state's pending operations: steps a run had started
    and never finished (`type`: creating, updating, deleting, reading, importing)."""
    return [{"type": str(op.get("type") or "?"), "urn": str((op.get("resource") or {}).get("urn") or "?")}
            for op in deployment.get("pending_operations") or []]


def refuse_pending(stack: str, deployment: dict[str, Any], key: Callable[[str], str]) -> None:
    """GuardError if the state holds a pending operation (see the module's docstring)."""
    ops = [{**op, "key": key(op["urn"])} for op in pending(deployment)]
    if not ops:
        return
    listed = "; ".join(f"{op['type']} {op['key']} ({op['urn']})" for op in ops)
    raise guard.GuardError(
        f"refused, nothing was applied: {stack}: the state holds {len(ops)} pending operation(s), left by a run "
        f"that was interrupted in the middle of a step: {listed}. Whether they happened is not known. "
        f"{PENDING_HOW}", {"pending": {stack: ops}})


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
            entry = plan.step(m, "done")
            if m.op != auto.OpType.SAME:
                ev.emit("infra", "done-step", stack=stack, op=str(m.op.value), urn=m.urn, key=entry["key"])
        elif e.res_op_failed_event:
            m = e.res_op_failed_event.metadata
            entry = plan.step(m, "failed")
            plan.error(m.urn, f"{m.op.value} failed")
            ev.emit("infra", "failed-step", stack=stack, op=str(m.op.value), urn=m.urn, key=entry["key"])
        elif e.diagnostic_event and e.diagnostic_event.severity in ("error", "warning"):
            d = e.diagnostic_event
            message = scrub_violation(d.message.strip())
            if d.severity == "error":
                plan.error(d.urn or "", message)
            ev.emit("infra", "diagnostic", stack=stack, severity=d.severity, urn=d.urn, message=message)
    return handle


def stopped(stack: str, verb: str, why: str, plan: guard.Plan, cmd: Any) -> dict[str, Any]:
    """Exactly what is known of a run that was stopped or failed part-way."""
    watch = getattr(cmd, "_watch", None)
    if watch is not None:
        watch.join(KILL_WAIT + 2)  # the engine is gone: let the watchdog say how
    prog = plan.progress()
    done, flying = prog["completed"], prog["in_flight"]
    say = lambda steps: ", ".join(f"{x['op']} {x['key']}" for x in steps)  # noqa: E731
    text = f"{stack}: the {verb} {why}. "
    if verb != "up":
        text += "A preview changes nothing."
    else:
        text += (f"Stopped after {say(done)} ({len(done)} step(s) completed). " if done
                 else "No step had completed. ")
        text += (f"In flight when it stopped: {say(flying)}; whether they happened is not known. " if flying
                 else "No step was in flight. ")
        text += ("The stack may be partly applied: preview it." if done or flying
                 else "Nothing was applied.")
        if flying:
            text += (f" If the engine did not get to finish its bookkeeping, the state holds a pending operation "
                     f"for {say(flying)}, and the next preview or deploy is refused until it is settled. "
                     f"{PENDING_HOW}.")
    outcome = getattr(cmd, "outcome", None)
    if outcome:
        text += f" The engine was {outcome}."
    return {"stack": stack, "verb": verb, "why": why, "completed": done, "in_flight": flying,
            "signals": list(getattr(cmd, "signals", []) or []), "engine": outcome,
            "partly_applied": verb == "up" and bool(done or flying), "text": text}


def engine(st: Any, ev: Emitter, stack: str, verb: str, plan: guard.Plan, refresh: bool = False,
           targets: list[str] | None = None, tripwire: Tripwire | None = None,
           plan_file: Path | None = None) -> dict[str, int]:
    """One `preview` or `up` of an open stack; its steps go into `plan`.
    `plan_file`: where a preview saves the engine's update plan, and what an
    up is bound to. -> the engine's own counts by op."""
    if verb == "up" and plan_file is None:
        raise guard.GuardError(f"{stack}: an up without an update plan is never run")
    cmd = st.workspace.pulumi_command
    tripped: list[str] = []
    asked = [0]

    def before_step(entry: dict[str, Any], op: str) -> None:
        why = tripwire(entry, op) if tripwire else None
        if why:
            if not tripped:
                # The step has started; what is left is to keep the engine
                # from the next one. Terminate at once.
                cmd.stop(hard=True)
            tripped.append(why)

    def on_cancel() -> None:
        asked[0] += 1
        cmd.stop(hard=asked[0] > 1)  # asked twice: do not wait for the step in flight

    remove = ev.on_cancel(on_cancel)
    try:
        ev.check()
        common: dict[str, Any] = dict(
            on_output=lambda line: ev.emit("infra", "log", stack=stack, line=scrub_violation(line.rstrip("\n"))),
            on_event=_on_event(ev, stack, plan, before_step),
            color="never",
            diff=True,
            refresh=refresh or None,
            target=targets or None,
            plan=str(plan_file) if plan_file else None,
        )
        ev.emit("infra", verb, stack=stack)
        try:
            if verb == "preview":
                changes = dict(st.preview(**common).change_summary)
            else:
                changes = dict(st.up(show_secrets=False, **common).summary.resource_changes or {})
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - reworded below, with what is known of the run
            # `from None` throughout: the SDK's error carries the engine's
            # whole output, violation values included, and a traceback of the
            # chain would print it.
            if tripped:
                info = stopped(stack, verb, "was stopped: it started a step its plan did not have ("
                               + "; ".join(tripped) + ")", plan, cmd)
                ev.emit("infra", "stopped", **info)
                raise guard.GuardError(info["text"], {"stopped": info}) from None
            if ev.cancelled:
                info = stopped(stack, verb, "was cancelled", plan, cmd)
                ev.emit("infra", "stopped", **info)
                raise Cancelled(info["text"], {"stopped": info}) from None
            if verb == "up" and plan.violations():
                info = stopped(stack, verb, "was refused by the engine: it left the plan that was checked ("
                               + "; ".join(plan.violations()) + ")", plan, cmd)
                ev.emit("infra", "stopped", **info)
                raise guard.GuardError(info["text"], {"stopped": info}) from None
            text = scrub_violation(f"{type(e).__name__}: {str(e).strip()}")
            flat = " ".join(text.split())
            said = [m for msgs in plan.errors.values() for m in msgs if m not in flat]
            raise EngineError("\n".join([text, *dict.fromkeys(said)])) from None
    finally:
        remove()
    if tripped:
        info = stopped(stack, verb, "started a step its plan did not have (" + "; ".join(tripped) + ")", plan, cmd)
        ev.emit("infra", "stopped", **info)
        raise guard.GuardError(info["text"], {"stopped": info})
    return {str(getattr(k, "value", k)): v for k, v in changes.items()}


def plan(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, preview: bool,
         refresh: bool = False, targets: list[str] | None = None,
         allow: guard.Allow | None = None, label: Callable[[str], str] | None = None,
         states: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Preview the stack and save the engine's update plan.
    -> {changes: counts, plan: [...], refused: [...], bound, unprotect}.
    `bound` is what apply() needs to run exactly this (None when the engine
    made no plan); `unprotect` the guests named for a delete that are protected
    in state. `allow` is scoped to the stack (guard.scope). `states`: the
    state resources of the other stacks of the run, by stack (guard.Held).

    Before the engine runs, the state is read: a pending operation refuses
    the run (refuse_pending). `preview` says whether a deploy follows: if one
    does (False), every guest in state that `allow` does not name for a
    replace or a delete, or that the program protects whatever is named, is
    protected in state first (guard.to_protect), a change of state only. A
    preview writes nothing."""
    allow = allow or guard.Allow()
    program = guard.load_program(wd)
    st = open_stack(s, wd, stack, env, ev)
    p = guard.Plan(s.stack, program)
    deployment = exported(st)
    refuse_pending(stack, deployment, p.key)
    mine = list(deployment.get("resources") or [])
    if not preview:
        failed = protect_state(st, ev, stack, guard.to_protect(mine, allow, p.key,
                                                               guard.protected_urns(s.stack, program)))
        if failed:
            raise EngineError(f"{stack}: nothing was applied: could not protect in state, before the plan: "
                              + "; ".join(f"{u} ({why})" for u, why in failed.items()))
    held = guard.Held({**(states or {}), stack: mine}, stack)
    plan_file = wd / PLAN_FILE
    plan_file.unlink(missing_ok=True)
    blocked = None
    try:
        counts = engine(st, ev, stack, "preview", p, refresh, targets, plan_file=plan_file)
    except EngineError as e:
        # `protect` makes a preview fail when the plan replaces or deletes a
        # protected resource. Its steps are all in the events all the same
        # (seen with a real engine), and it wrote no plan, so nothing can be
        # applied from it. The guard below says what is refused, by name.
        if not p.only_protect_errors():
            raise
        blocked, counts = e, p.counts()
        plan_file.unlink(missing_ok=True)
    changes = p.changes()
    refused = (guard.import_refusals(program) + guard.unknown_refusals(program, changes)
               + guard.refusals(changes, allow, label, held))
    refused_urns = {r.get("urn") for r in refused}
    # A guest the request names for a delete, that the program no longer has
    # and the state protects (an earlier run of this runner set it): the
    # engine will not delete it until it is unprotected in state, which
    # apply() does, for exactly these.
    unprotect = [c["urn"] for c in changes if c.get("protected") and c["urn"] not in refused_urns
                 and guard.gated(c["type"]) and c["op"] == "delete"
                 and (c["key"] in allow.delete or c["urn"] in allow.delete)]
    if blocked is not None:
        unexplained = p.protected() - refused_urns - set(unprotect)
        if unexplained:
            # Protected by the estate itself, or not a guest: the engine's word stands.
            raise blocked
    bound = None
    if blocked is None:
        if not plan_file.is_file():
            raise EngineError(f"{stack}: the preview wrote no update plan to {plan_file}; "
                              f"an up without one is never run")
        os.chmod(plan_file, 0o600)
        bound = {"plan_file": str(plan_file), "plan_sha256": guard.sha256(plan_file),
                 "program_sha256": guard.sha256(wd / "Pulumi.yaml"), "refresh": bool(refresh),
                 "targets": list(targets or [])}
    ev.emit("infra", "plan", stack=stack, preview=preview, changes=changes, refused=refused,
            text=guard.lines(stack, changes, refused, preview))
    ev.emit("infra", "summary", stack=stack, changes=counts, resources=guard.by_op(changes), refused=refused)
    return {"changes": counts, "plan": changes, "refused": refused, "bound": bound, "unprotect": unprotect}


def _gated(changes: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted((c["urn"], f) for c in changes if guard.gated(c["type"])
                  for f in {guard.gate(c["type"], x) for x in c["steps"]} if f)


def unprotect(st: Any, urn: str) -> None:
    """`pulumi state unprotect`: the engine refuses to delete a resource the
    state protects, and a program that no longer has it cannot say otherwise."""
    st._run_pulumi_cmd_sync(["state", "unprotect", urn, "--yes"])  # noqa: SLF001 - no public call for it


def protect_state(st: Any, ev: Emitter, stack: str, urns: list[str]) -> dict[str, str]:
    """`pulumi state protect` on each URN: a change of the state only, nothing
    real is touched. One call each, so that one that fails does not keep the
    others unprotected. -> {urn: why} of those that could not be protected."""
    failed: dict[str, str] = {}
    for urn in urns:
        try:
            st._run_pulumi_cmd_sync(["state", "protect", urn, "--yes"])  # noqa: SLF001 - no public call for it
            ev.emit("infra", "protect", stack=stack, urn=urn)
        except Exception as e:  # noqa: BLE001 - reported by the caller, per URN
            failed[urn] = " ".join(scrub_violation(str(e)).split())[-400:] or type(e).__name__
    return failed


def reprotect(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, program: dict[str, Any],
              unprotected: list[str]) -> Optional[str]:
    """After an up, whatever became of it: protect in state every guest that is
    still there and not protected: one this run unprotected for a delete that
    did not happen (the up failed, was cancelled or refused), one it replaced
    (the program that ran did not protect it), one a failed run left behind.
    Best effort. -> None, or what could not be done, which is also an `error`
    event: the caller must not let it pass quietly."""
    key = guard.Plan(s.stack, program).key
    try:
        st = _stack(s, wd, env)  # not open_stack: this runs after a cancel too
        resources = state(st)
        failed = protect_state(st, ev, stack, guard.to_protect(resources, guard.Allow(), key))
        if not failed:
            return None
        what = "; ".join(f"{u} ({why})" for u, why in failed.items())
    except Exception as e:  # noqa: BLE001 - reported, never raised over the up's own outcome
        what = (f"the state could not be read ({type(e).__name__}: {' '.join(scrub_violation(str(e)).split())[-400:]})"
                + (f"; this run unprotected {', '.join(unprotected)}" if unprotected else ""))
    text = (f"{stack}: GUESTS ARE LEFT UNPROTECTED IN STATE: {what}. Until they are protected again only the "
            f"plan check stands between a deploy and their deletion. Protect each that is still in state: "
            f"`pulumi state protect '<urn>'` (with the stack's backend and passphrase)")
    ev.emit("infra", "error", stack=stack, text=text, unprotected=unprotected)
    return text


def apply(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, planned: dict[str, Any],
          refresh: bool = False, targets: list[str] | None = None,
          allow: guard.Allow | None = None, label: Callable[[str], str] | None = None,
          states: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """`up` a stack, bound to the plan that passed (`planned`: what plan()
    returned for this directory). -> {changes: counts, plan: what it did,
    unprotected?: what could not be protected in state again}."""
    allow = allow or guard.Allow()
    program = guard.load_program(wd)
    if guard.imports(program):
        raise guard.GuardError(guard.message({stack: guard.import_refusals(program)}))
    if planned.get("refused"):
        raise guard.GuardError(guard.message({stack: planned["refused"]}))
    # From here on the state may be changed (a named delete is unprotected in
    # it) and an up may run: whatever comes of it, a refusal, a failure, a
    # cancel or a success, every guest still in state ends protected.
    unprotected: list[str] = []
    try:
        counts, p = _apply(s, wd, stack, env, ev, planned, refresh, targets, allow, label, states, program,
                           unprotected)
    except BaseException as e:
        left = reprotect(s, wd, stack, env, ev, program, unprotected)
        if left and e.args and isinstance(e.args[0], str):
            e.args = (f"{e.args[0]}\n{left}", *e.args[1:])
        raise
    left = reprotect(s, wd, stack, env, ev, program, unprotected)
    changes = p.changes()
    ev.emit("infra", "summary", stack=stack, changes=counts, resources=guard.by_op(changes), refused=[])
    return {"changes": counts, "plan": changes, **({"unprotected": left} if left else {})}


def _apply(s: Settings, wd: Path, stack: str, env: dict[str, str], ev: Emitter, planned: dict[str, Any],
           refresh: bool, targets: list[str] | None, allow: guard.Allow, label: Callable[[str], str] | None,
           states: dict[str, list[dict[str, Any]]] | None, program: dict[str, Any],
           unprotected: list[str]) -> tuple[dict[str, int], guard.Plan]:
    """The unprotecting, the second plan and the up of apply(). `unprotected` collects the URNs it
    unprotected in state, for apply() to protect again whatever happens here."""
    if planned.get("unprotect"):
        st = open_stack(s, wd, stack, env, ev, install=False)
        for urn in planned["unprotect"]:
            ev.emit("infra", "unprotect", stack=stack, urn=urn)
            unprotected.append(urn)  # before the call: whether it got there or not
            unprotect(st, urn)
        again = plan(s, wd, stack, env, ev, False, refresh, targets, allow, label, states)
        if again["refused"] or again["unprotect"] or _gated(again["plan"]) != _gated(planned["plan"]):
            raise guard.GuardError(
                f"refused: {stack}: after unprotecting {', '.join(planned['unprotect'])} the plan is not the "
                f"one that was checked; nothing was applied (they are protected in state again)",
                {"plan": {stack: again["plan"]}, "refused": {stack: again["refused"]}})
        planned = again
    bound = planned.get("bound")
    if not bound:
        raise guard.GuardError(f"refused: {stack}: no update plan was made for this run; "
                               f"an up without one is never run")
    plan_file = Path(bound["plan_file"])
    if plan_file.parent != wd or bound["refresh"] != bool(refresh) or bound["targets"] != list(targets or []):
        raise guard.GuardError(f"refused: {stack}: the plan was made for another run "
                               f"(directory, targets or refresh differ)")
    if guard.sha256(wd / "Pulumi.yaml") != bound["program_sha256"]:
        raise guard.GuardError(f"refused, nothing was applied: {stack}: {wd / 'Pulumi.yaml'} changed between "
                               f"the plan and the up")
    if not plan_file.is_file() or guard.sha256(plan_file) != bound["plan_sha256"]:
        raise guard.GuardError(f"refused, nothing was applied: {stack}: the update plan {plan_file} changed "
                               f"between the plan and the up")
    st = open_stack(s, wd, stack, env, ev, install=False)
    p = guard.Plan(s.stack, program)

    def tripwire(entry: dict[str, Any], op: str) -> Optional[str]:
        bad = guard.refusals([{**entry, "steps": [op]}], allow, label)
        return f"{op} of guest {entry['key']} (needs {bad[0]['flag']})" if bad else None

    try:
        counts = engine(st, ev, stack, "up", p, refresh, targets, tripwire, plan_file)
    except EngineError as e:
        info = stopped(stack, "up", "failed", p, st.workspace.pulumi_command)
        ev.emit("infra", "stopped", **info)
        err = EngineError(f"{e}\n{info['text']}")
        err.result = {"stopped": info}  # type: ignore[attr-defined]
        raise err from None
    finally:
        plan_file.unlink(missing_ok=True)  # one plan, one up
    return counts, p
