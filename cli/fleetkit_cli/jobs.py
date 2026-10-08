"""Deploy jobs for the API: one at a time per estate, each with an append-only
event log on disk (jobs/<id>.jsonl) and a record (jobs/<id>.json).

A second deploy of an estate that is still running is refused (BusyError), not
queued: a caller that retries decides what to do, and nothing deploys an
estate twice behind anyone's back. A job found running at startup was cut off
by a restart and is marked interrupted.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .events import Cancelled, Emitter
from .pipeline import DeployRequest

Runner = Callable[[DeployRequest, Emitter], dict[str, Any]]
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


class BusyError(Exception):
    pass


class Job:
    def __init__(self, jid: str, req: DeployRequest, d: Path):
        self.id = jid
        self.request = req
        self.state = "queued"
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self.emitter: Emitter | None = None
        self._dir = d
        self._cond = threading.Condition()

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id, "state": self.state, "request": self.request.model_dump(),
            "created": self.created, "started": self.started, "finished": self.finished,
            "result": self.result, "error": self.error, "events": len(self.events),
        }

    def save(self) -> None:
        tmp = self._dir / f"{self.id}.json.tmp"
        tmp.write_text(json.dumps(self.record(), indent=1))
        tmp.replace(self._dir / f"{self.id}.json")

    def add(self, e: dict[str, Any]) -> None:
        with self._cond:
            e = {"seq": len(self.events), **e}
            self.events.append(e)
            with open(self._dir / f"{self.id}.jsonl", "a") as f:
                f.write(json.dumps(e) + "\n")
            self._cond.notify_all()

    def wait_events(self, after: int, timeout: float) -> list[dict[str, Any]]:
        """Events with seq >= after; blocks up to timeout for new ones while running."""
        with self._cond:
            if len(self.events) <= after and self.state not in TERMINAL:
                self._cond.wait(timeout)
            return self.events[after:]

    def set_state(self, state: str) -> None:
        with self._cond:
            self.state = state
            self.save()
            self._cond.notify_all()


class JobManager:
    def __init__(self, state_dir: Path, runner: Runner, workers: int = 4):
        self.dir = state_dir / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self.jobs: dict[str, Job] = {}
        self.active: dict[str, str] = {}  # estate -> job id
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deploy")
        # Called with each job once it is final (GitOps reports PR results and
        # starts what was queued behind it).
        self.on_finish: list[Callable[[Job], None]] = []
        self._load()

    def _load(self) -> None:
        for f in sorted(self.dir.glob("*.json")):
            try:
                r = json.loads(f.read_text())
                j = Job(r["id"], DeployRequest(**r["request"]), self.dir)
                j.state, j.created, j.started, j.finished = r["state"], r["created"], r["started"], r["finished"]
                j.result, j.error = r.get("result"), r.get("error")
                log = self.dir / f"{j.id}.jsonl"
                if log.exists():
                    j.events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
                if j.state not in TERMINAL:
                    j.state, j.error, j.finished = "interrupted", "the server stopped during the job", time.time()
                    j.save()
                self.jobs[j.id] = j
            except Exception:  # noqa: BLE001 - one unreadable record must not stop the server
                continue

    def submit(self, req: DeployRequest) -> Job:
        with self.lock:
            busy = self.active.get(req.estate)
            if busy:
                raise BusyError(busy)
            j = Job(uuid.uuid4().hex[:12], req, self.dir)
            self.jobs[j.id] = j
            self.active[req.estate] = j.id
            j.emitter = Emitter(j.add)
            j.save()
        self.pool.submit(self._run, j)
        return j

    def _run(self, j: Job) -> None:
        assert j.emitter is not None
        j.started = time.time()
        j.set_state("running")
        try:
            j.result = self.runner(j.request, j.emitter)
            final = "succeeded"
        except Cancelled as e:
            # What is known of a cancelled run: its plan, and for an `up` that
            # had started, the steps that finished and those in flight.
            if isinstance(e.result, dict):
                j.result = j.emitter.redact(e.result)
            if str(e) != "cancelled":
                j.error = j.emitter.redact(str(e))
            final = "cancelled"
        except Exception as e:  # noqa: BLE001 - every failure is reported on the job
            # The message of a failing tool can carry a decrypted secret too.
            j.error = j.emitter.redact(f"{type(e).__name__}: {e}")
            # A refused deploy (guard.GuardError) still says what it would have
            # done: the plan and the refused resources.
            partial = getattr(e, "result", None)
            if isinstance(partial, dict):
                j.result = j.emitter.redact(partial)
            j.emitter.emit("job", "error", error=j.error, trace=traceback.format_exc()[-4000:])
            final = "cancelled" if j.emitter.cancelled else "failed"
        finally:
            with self.lock:
                self.active.pop(j.request.estate, None)
        j.finished = time.time()
        j.emitter.emit("job", final)
        j.set_state(final)
        for hook in list(self.on_finish):
            try:
                hook(j)
            except Exception:  # noqa: BLE001 - a hook must not break the worker
                traceback.print_exc()

    def cancel(self, jid: str) -> Job | None:
        """Ask the job to stop. Its engine (the pulumi process it started) is
        signalled: it finishes the step in flight and starts no other. A
        second cancel makes it terminate at once. Never `pulumi cancel`."""
        j = self.jobs.get(jid)
        if j and j.state not in TERMINAL and j.emitter:
            j.emitter.cancel()
        return j

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
