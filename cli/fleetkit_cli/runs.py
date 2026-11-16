"""Deploy on merge, verified live by a reviewer (FLEETKIT_TRIGGER=verified;
docs/pipeline.md, "Verified runs").

The deploy branch (FLEETKIT_BRANCH, e.g. unstable) deploys; FLEETKIT_STABLE_BRANCH
(default stable) records the last commit a reviewer passed, and is what a
failed run returns the estates to.

  trigger   a merged pull request (webhook) or POST /v1/runs {repo, sha}. Refused,
            and alerted, unless `sha` is the merge commit of a pull request into
            the deploy branch whose required checks (FLEETKIT_REQUIRED_CHECKS) are
            green on its head: a direct push never deploys.
  queue     one run per repository. Merges that arrive meanwhile queue; the next
            run takes the newest and covers the whole range stable..sha.
  run       preview every estate; a delete or replace, a guard refusal, or any
            change to a resource matching FLEETKIT_PROTECT stops the run
            (needs-operator) before anything is applied. Then deploy every
            estate, then dispatch review.requested to the runner adapter.
  verdict   POST /v1/runs/{id}/verdict. pass: fast-forward stable to the run's
            commit, comment the evidence on each ticket, set it done. fail:
            redeploy the stable commit, open a pull request reverting the range
            on the deploy branch, reopen each ticket with the findings, dispatch
            work.requested; the queue waits until that revert is merged. A ticket
            labelled no-auto-revert, a revert that conflicts, or a failed
            redeploy stops the queue for a person instead.
  timeout   no verdict within FLEETKIT_REVIEW_TIMEOUT seconds (default 1800):
            the run is timed-out and the queue stops. Nothing is reverted.

A stopped queue (needs-operator, timed-out) moves again with POST
/v1/runs/{id}/resolve (a person decided) or /review (ask for a verdict again).

The adapters are interfaces: Forge (GitHub), Tracker (Jira), Runner (exec, http),
Deployer (the server's JobManager), Git (the server's mirror); adapters.py has
the real ones, tests/test_runs.py fakes them.
"""
from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import guard

# The run is going, or waits for a person: either way the repository's queue waits.
BLOCKING = ("previewing", "deploying", "reviewing", "passing", "rolling-back", "reverting",
            "needs-operator", "timed-out")
DONE = ("passed", "failed", "refused", "resolved")
OK_CHECK = ("success", "neutral", "skipped")
KEEP = 500  # finished runs kept in runs.json


class Forge(Protocol):
    def merged_pr(self, repo: str, sha: str) -> Optional[dict[str, Any]]: ...
    def checks(self, repo: str, sha: str) -> dict[str, str]: ...
    def fast_forward(self, repo: str, branch: str, sha: str) -> None: ...
    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict[str, Any]: ...


class Tracker(Protocol):
    pattern: str

    def get(self, key: str) -> dict[str, Any]: ...
    def comment(self, key: str, text: str) -> None: ...
    def set_state(self, key: str, state: str) -> None: ...
    def link(self, key: str, url: str, title: str) -> None: ...


class Runner(Protocol):
    def dispatch(self, event: dict[str, Any]) -> None: ...


class Deployer(Protocol):
    def deploy(self, estate: str, sha: str, preview: bool) -> dict[str, Any]:
        """-> {"state": job state, "job": id, "result": ..., "error": ...}; waits for the end."""
        ...


class Git(Protocol):
    def head(self, branch: str) -> str: ...
    def messages(self, base: str, sha: str) -> str: ...
    def revert(self, base: str, sha: str, branch: str, message: str) -> None: ...


class Pipeline:
    def __init__(self, state_dir: Path, branch: str, estates: list[str], forge: Forge, tracker: Optional[Tracker],
                 runner: Optional[Runner], deployer: Deployer, git: Git, *, stable: str = "stable",
                 required: tuple[str, ...] = (), protect: tuple[str, ...] = (), timeout: float = 1800,
                 done_state: str = "Done", reopen_state: str = "To Do", public_url: Optional[str] = None,
                 alert: Callable[[str], None] = lambda msg: None,
                 spawn: Optional[Callable[[Callable[[], None]], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.file = state_dir / "runs.json"
        self.branch, self.stable, self.estates = branch, stable, estates
        self.forge, self.tracker, self.runner, self.deployer, self.git = forge, tracker, runner, deployer, git
        self.required, self.protect, self.timeout = required, protect, timeout
        self.done_state, self.reopen_state, self.public_url = done_state, reopen_state, public_url
        self._alert, self.clock = alert, clock
        self.spawn = spawn or (lambda f: threading.Thread(target=f, name="run", daemon=True).start())
        self.lock = threading.RLock()
        self.state: dict[str, Any] = json.loads(self.file.read_text()) if self.file.exists() else {}
        self.runs: dict[str, dict[str, Any]] = self.state.setdefault("runs", {})
        self.queued: dict[str, dict[str, Any]] = self.state.setdefault("queued", {})
        for r in self.runs.values():
            # Cut off by a restart in the middle of a deploy: a person looks at it.
            if r["state"] in ("previewing", "deploying", "passing", "rolling-back"):
                self._log(r, f"the server stopped while {r['state']}")
                r["state"] = "needs-operator"
        self._save()

    # ── bookkeeping ──────────────────────────────────────────────────────
    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.file)

    def _log(self, r: dict[str, Any], msg: str) -> None:
        r.setdefault("log", []).append({"at": self.clock(), "msg": msg})

    def _set(self, r: dict[str, Any], state: str, msg: str, alert: bool = False) -> None:
        with self.lock:
            r["state"] = state
            r["updated"] = self.clock()
            self._log(r, msg)
            self._save()
        if alert:
            self.alert(f"fleetkit run {r['id']} ({r['repo']} {r.get('sha', '')[:10]}): {state}: {msg}")

    def alert(self, msg: str) -> None:
        try:
            self._alert(msg)
        except Exception:  # noqa: BLE001 - an alert that does not go out must not stop the run
            traceback.print_exc()

    def url(self, r: dict[str, Any]) -> str:
        return f"{self.public_url.rstrip('/')}/v1/runs/{r['id']}" if self.public_url else f"run {r['id']}"

    def get(self, rid: str) -> Optional[dict[str, Any]]:
        return self.runs.get(rid)

    def list(self, state: Optional[str] = None) -> list[dict[str, Any]]:
        rs = sorted(self.runs.values(), key=lambda r: r["created"], reverse=True)
        return [r for r in rs if state is None or r["state"] == state]

    def _new(self, repo: str, sha: str, pr: Optional[int], state: str) -> dict[str, Any]:
        now = self.clock()
        r = {"id": uuid.uuid4().hex[:12], "repo": repo, "sha": sha, "pr": pr, "state": state,
             "created": now, "updated": now, "log": []}
        self.runs[r["id"]] = r
        # The record stays bounded: the oldest finished runs go first.
        for old in sorted((x for x in self.runs.values() if x["state"] in DONE), key=lambda x: x["created"])[:-KEEP]:
            del self.runs[old["id"]]
        return r

    # ── trigger and queue ────────────────────────────────────────────────
    def trigger(self, repo: str, sha: str, source: str) -> dict[str, Any]:
        """A merge reached the deploy branch. -> the refused run, or the queue."""
        why, pr = self._refusal(repo, sha)
        if why:
            with self.lock:
                r = self._new(repo, sha, pr, "refused")
            self._set(r, "refused", f"{source}: {why}; nothing deploys", alert=True)
            return {"refused": why, "run": r}
        with self.lock:
            # The merge of a failed run's revert lets the queue move again.
            for r in self.runs.values():
                if r["repo"] == repo and r["state"] == "reverting" and r.get("revert_pr") == pr:
                    self._set(r, "failed", f"the revert (#{pr}) is merged")
            self.queued[repo] = {"sha": sha, "pr": pr, "at": self.clock(), "source": source}
            self._save()
        started = self._advance(repo)
        return {"queued": sha, "started": started}

    def _refusal(self, repo: str, sha: str) -> tuple[Optional[str], Optional[int]]:
        pr = self.forge.merged_pr(repo, sha)
        if not pr:
            return f"{sha[:12]} is not the merge commit of a pull request (a direct push?)", None
        n = pr.get("number")
        if (pr.get("base") or {}).get("ref") != self.branch:
            return f"#{n} was merged into {(pr.get('base') or {}).get('ref')}, not {self.branch}", n
        checks = self.forge.checks(repo, (pr.get("head") or {}).get("sha", ""))
        bad = [f"{c}: {checks.get(c, 'missing')}" for c in self.required if checks.get(c) not in OK_CHECK]
        if bad:
            return f"#{n} has required checks that are not green ({', '.join(bad)})", n
        return None, n

    def _advance(self, repo: str) -> Optional[str]:
        with self.lock:
            if any(r["repo"] == repo and r["state"] in BLOCKING for r in self.runs.values()):
                return None
            q = self.queued.pop(repo, None)
            if q is None:
                self._save()
                return None
            r = self._new(repo, q["sha"], q["pr"], "previewing")
            r["base"] = self.git.head(self.stable)
            self._log(r, f"range {r['base'][:12]}..{q['sha'][:12]} ({q['source']})")
            self._save()
        self.spawn(lambda: self._guarded(r, self._go))
        return r["id"]

    def _guarded(self, r: dict[str, Any], step: Callable[[dict[str, Any]], None]) -> None:
        try:
            step(r)
        except Exception as e:  # noqa: BLE001 - whatever breaks, a person looks at it
            traceback.print_exc()
            self._set(r, "needs-operator", f"{type(e).__name__}: {e}", alert=True)

    # ── a run ────────────────────────────────────────────────────────────
    def _go(self, r: dict[str, Any]) -> None:
        jobs = r.setdefault("jobs", {})
        for e in self.estates:
            out = self.deployer.deploy(e, r["sha"], preview=True)
            jobs[f"preview:{e}"] = out.get("job")
            if out["state"] != "succeeded":
                return self._set(r, "needs-operator", f"preview of {e} {out['state']}: {out.get('error')}", True)
            hits = self.needs_operator(out.get("result") or {})
            if hits:
                return self._set(r, "needs-operator", f"{e} needs a person: {'; '.join(hits)}", True)
        self._set(r, "deploying", "previews clean")
        for e in self.estates:
            out = self.deployer.deploy(e, r["sha"], preview=False)
            jobs[f"deploy:{e}"] = out.get("job")
            if out["state"] != "succeeded":
                return self._set(r, "needs-operator", f"deploy of {e} {out['state']}: {out.get('error')}", True)
        r["tickets"] = self.tickets(r)
        r["deadline"] = self.clock() + self.timeout
        self._set(r, "reviewing", f"deployed; review of {', '.join(r['tickets']) or 'no tickets'} requested")
        self._dispatch("review.requested", r)

    def needs_operator(self, result: dict[str, Any]) -> list[str]:
        """What in a preview's result stops a run: a refusal, a delete or
        replace, a change to a protected resource.
        shortcut: sees the Pulumi plan only, not what Colmena would change on a
        protected node; compare toplevels before a protected node is in the hive."""
        hits = [f"refused {x['op']} {st}/{x['key']}" for st, xs in (result.get("refused") or {}).items() for x in xs]
        for st, changes in (result.get("plan") or {}).items():
            for c in changes:
                name = f"{st}/{c['key']}"
                if guard.family(c["op"]) in ("delete", "replace"):
                    hits.append(f"{c['op']} {name}")
                elif c["op"] != "same" and any(fnmatch.fnmatch(name, p) for p in self.protect):
                    hits.append(f"{c['op']} {name} (protected)")
        return hits

    def tickets(self, r: dict[str, Any]) -> list[str]:
        if not self.tracker:
            return []
        text = self.git.messages(r["base"], r["sha"])
        return sorted(set(re.findall(self.tracker.pattern, text)))

    def _dispatch(self, kind: str, r: dict[str, Any], **extra: Any) -> None:
        if not self.runner:
            return
        try:
            self.runner.dispatch({"event": kind, "run": r["id"], "repo": r["repo"], "url": self.url(r),
                                  "range": {"base": r.get("base"), "head": r["sha"]},
                                  "tickets": r.get("tickets", []), **extra})
        except Exception as e:  # noqa: BLE001 - recorded; /review asks again
            self._set(r, r["state"], f"{kind} not dispatched: {e}", alert=True)

    def _each_ticket(self, r: dict[str, Any], f: Callable[[Tracker, str], None]) -> None:
        if not self.tracker:
            return
        for t in r.get("tickets", []):
            try:
                f(self.tracker, t)
            except Exception as e:  # noqa: BLE001 - the run's outcome stands; the ticket is a person's fix
                self._set(r, r["state"], f"ticket {t}: {e}", alert=True)

    # ── verdict, review, timeout, resolve ────────────────────────────────
    def verdict(self, rid: str, verdict: str, evidence: str, by: str) -> dict[str, Any]:
        with self.lock:
            r = self.runs[rid]
            if r["state"] != "reviewing":
                raise ValueError(f"run {rid} is {r['state']}, not reviewing")
            r["verdict"] = {"verdict": verdict, "evidence": evidence, "by": by, "at": self.clock()}
            # Taken before the step runs, so a second verdict is refused.
            r["state"] = "passing" if verdict == "pass" else "rolling-back"
            self._save()
        if verdict == "pass":
            self._guarded(r, self._pass)
        else:
            self.spawn(lambda: self._guarded(r, self._fail))
        return r

    def _pass(self, r: dict[str, Any]) -> None:
        try:
            self.forge.fast_forward(r["repo"], self.stable, r["sha"])
        except Exception as e:  # noqa: BLE001 - stable did not move: a person decides
            return self._set(r, "needs-operator", f"passed, but {self.stable} did not move: {e}", True)
        note = f"Verified live by {r['verdict']['by']} ({self.url(r)}); {self.stable} is now {r['sha'][:12]}.\n\n" \
               f"{r['verdict']['evidence']}"
        self._each_ticket(r, lambda t, k: (t.comment(k, note), t.set_state(k, self.done_state)))
        self._set(r, "passed", f"{self.stable} fast-forwarded to {r['sha'][:12]}")
        self._advance(r["repo"])

    def _fail(self, r: dict[str, Any]) -> None:
        held = [k for k in r.get("tickets", []) if self.tracker and "no-auto-revert" in
                (self.tracker.get(k).get("labels") or [])]
        if held:
            return self._set(r, "needs-operator", f"failed review; no-auto-revert on {', '.join(held)}", True)
        # First the estates go back to the last verified commit, then the history.
        for e in self.estates:
            out = self.deployer.deploy(e, r["base"], preview=False)
            r.setdefault("jobs", {})[f"rollback:{e}"] = out.get("job")
            if out["state"] != "succeeded":
                return self._set(r, "needs-operator",
                                 f"failed review, and the redeploy of {r['base'][:12]} to {e} {out['state']}: "
                                 f"{out.get('error')}", True)
        self._set(r, "rolling-back", f"estates back at {self.stable} {r['base'][:12]}")
        name = f"fleetkit/revert-{r['id']}"
        title = f"Revert {r['base'][:10]}..{r['sha'][:10]}: failed live review (run {r['id']})"
        try:
            self.git.revert(r["base"], r["sha"], name, title)
            pr = self.forge.open_pr(r["repo"], name, self.branch, title,
                                    f"The reviewer failed run {self.url(r)}:\n\n{r['verdict']['evidence']}")
        except Exception as e:  # noqa: BLE001 - a conflict or a refused push: a person decides
            return self._set(r, "needs-operator", f"failed review; the revert could not be opened: {e}", True)
        r["revert_pr"], r["revert_url"] = pr.get("number"), pr.get("html_url")
        note = f"Failed live review ({self.url(r)}); the estates are back at {self.stable} {r['base'][:12]} " \
               f"and {pr.get('html_url')} reverts the change.\n\n{r['verdict']['evidence']}"
        self._each_ticket(r, lambda t, k: (t.comment(k, note), t.set_state(k, self.reopen_state),
                                           t.link(k, pr.get("html_url") or "", title)))
        self._set(r, "reverting", f"revert #{pr.get('number')} opened; the queue waits for its merge")
        self._dispatch("work.requested", r, findings=r["verdict"]["evidence"], revert=pr.get("html_url"))

    def review(self, rid: str) -> dict[str, Any]:
        """Ask for a verdict (again): a timed-out run is reviewed anew."""
        with self.lock:
            r = self.runs[rid]
            if r["state"] not in ("reviewing", "timed-out"):
                raise ValueError(f"run {rid} is {r['state']}; only a deployed run is reviewed")
            r["deadline"] = self.clock() + self.timeout
        self._set(r, "reviewing", "review requested again")
        self._dispatch("review.requested", r)
        return r

    def tick(self) -> None:
        """Time out reviews. Never reverts: a reviewer outage must not undo good changes."""
        for r in [r for r in self.runs.values() if r["state"] == "reviewing"]:
            if self.clock() > r.get("deadline", float("inf")):
                self._set(r, "timed-out", "no verdict in time; the queue stops (nothing reverted)", alert=True)

    def resolve(self, rid: str, note: str, by: str) -> dict[str, Any]:
        """A person decided about a stopped run; the queue moves on."""
        with self.lock:
            r = self.runs[rid]
            if r["state"] not in ("needs-operator", "timed-out", "reverting"):
                raise ValueError(f"run {rid} is {r['state']}; nothing to resolve")
        self._set(r, "resolved", f"resolved by {by}: {note}")
        self._advance(r["repo"])
        return r

    def start_ticking(self, every: float = 30) -> None:
        def loop() -> None:
            while True:
                time.sleep(every)
                self.tick()

        threading.Thread(target=loop, name="runs-tick", daemon=True).start()
