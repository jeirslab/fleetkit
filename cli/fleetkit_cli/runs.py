"""Deploy on merge, verified live by a reviewer (FLEETKIT_TRIGGER=verified;
docs/pulumi.md, "Verified runs").

The deploy branch (FLEETKIT_BRANCH, e.g. unstable) deploys; FLEETKIT_STABLE_BRANCH
(default stable) records the last commit a reviewer passed, and is what a
failed run returns the estates to. One repository (FLEETKIT_GITHUB_REPO).

  trigger   a merged pull request (webhook) or POST /v1/runs {repo, sha}. Refused,
            and alerted, unless `sha` is the merge commit of a pull request into
            the deploy branch whose required checks (FLEETKIT_REQUIRED_CHECKS,
            check-run names) are green on its head, descends from stable, and is
            newer than the last commit a run deployed.
  queue     one run per repository. Merges that arrive meanwhile queue; the next
            run takes the newest and covers the whole range stable..sha. Every
            first-parent commit of that range must pass the same guard: a direct
            push folded into a later merge stops the run for a person.
  run       preview every estate; a delete or replace, a guard refusal, or any
            change to a resource matching FLEETKIT_PROTECT stops the run
            (needs-operator) before anything is applied. So does a protected
            node of FLEETKIT_VERIFIED_ON (tagged mainnet, or its name or a tag
            matching a FLEETKIT_PROTECT pattern's last segment) whose built
            toplevel is not the one it runs live, or whose live one cannot be
            read. Then deploy every estate, then dispatch review.requested to
            the runner adapter. Colmena applies only to the nodes
            FLEETKIT_VERIFIED_ON selects (none by default).
  verdict   POST /v1/runs/{id}/verdict. pass: fast-forward stable to the run's
            commit, comment the evidence on each ticket, set it done. fail:
            preview and redeploy the stable commit (the same gate as a run),
            open a pull request reverting the range on the deploy branch, reopen
            each ticket with the findings, dispatch work.requested; the queue
            waits until that revert is merged (by a person or an agent; its
            checks gate it like any merge). A ticket labelled no-auto-revert, a
            revert that conflicts, or a rollback the gate stops waits for a
            person instead.
  timeout   no verdict within FLEETKIT_REVIEW_TIMEOUT seconds (default 1800):
            the run is timed-out and the queue stops. Nothing is reverted. A
            revert pull request left unmerged that long is alerted.

A stopped queue moves again with POST /v1/runs/{id}/resolve (a person decided;
`accept` vouches for a range the guard refused, `stable` moves stable to the
run's commit) or /review (ask for a verdict again).

The adapters are interfaces: Forge (GitHub), Tracker (Jira), Runner (exec, http),
Deployer (the server's JobManager), Git (the server's mirror); adapters.py has
the real ones, tests/test_runs.py fakes them.
"""
from __future__ import annotations

import copy
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
GREEN_KEEP = 2000  # commits known to have passed the guard


class Forge(Protocol):
    def merged_pr(self, repo: str, sha: str) -> Optional[dict[str, Any]]: ...
    def checks(self, repo: str, sha: str) -> dict[str, str]: ...
    def fast_forward(self, repo: str, branch: str, sha: str) -> None: ...
    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict[str, Any]: ...


class Tracker(Protocol):
    pattern: str

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """None: no such ticket. Any other failure raises."""
        ...
    def comment(self, key: str, text: str) -> None: ...
    def set_state(self, key: str, state: str) -> None: ...
    def link(self, key: str, url: str, title: str) -> None: ...


class Runner(Protocol):
    def dispatch(self, event: dict[str, Any]) -> None: ...


class Deployer(Protocol):
    def deploy(self, estate: str, sha: str, preview: bool, on: tuple[str, ...]) -> dict[str, Any]:
        """-> {"state": job state, "job": id, "result": ..., "error": ...}; waits for the end.
        `on`: the Colmena nodes (empty: no NixOS stage)."""
        ...


class Git(Protocol):
    def head(self, branch: str) -> str: ...
    def is_ancestor(self, a: str, b: str) -> bool: ...
    def first_parents(self, base: str, sha: str) -> list[str]: ...
    def log(self, base: str, sha: str) -> list[tuple[str, str]]: ...
    def same_tree(self, a: str, b: str) -> bool: ...
    def revert(self, base: str, sha: str, branch: str, message: str) -> str: ...


class Pipeline:
    def __init__(self, state_dir: Path, repo: str, branch: str, estates: list[str], forge: Forge,
                 tracker: Optional[Tracker], runner: Optional[Runner], deployer: Deployer, git: Git, *,
                 stable: str = "stable", required: tuple[str, ...] = (), protect: tuple[str, ...] = (),
                 nixos_on: tuple[str, ...] = (), timeout: float = 1800, done_state: str = "Done",
                 reopen_state: str = "To Do", public_url: Optional[str] = None,
                 alert: Callable[[str], None] = lambda msg: None,
                 spawn: Optional[Callable[[Callable[[], None]], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.file = state_dir / "runs.json"
        self.repo, self.branch, self.stable, self.estates = repo, branch, stable, estates
        self.forge, self.tracker, self.runner, self.deployer, self.git = forge, tracker, runner, deployer, git
        self.required, self.protect, self.nixos_on, self.timeout = required, protect, nixos_on, timeout
        self.done_state, self.reopen_state, self.public_url = done_state, reopen_state, public_url
        self._alert, self.clock = alert, clock
        self.spawn = spawn or (lambda f: threading.Thread(target=f, name="run", daemon=True).start())
        self.lock = threading.RLock()
        self.state: dict[str, Any] = json.loads(self.file.read_text()) if self.file.exists() else {}
        self.runs: dict[str, dict[str, Any]] = self.state.setdefault("runs", {})
        self.green: list[str] = self.state.setdefault("green", [])
        # Commits a failed run reverted, and its revert: their keys are not a later run's tickets.
        self.reverted: list[str] = self.state.setdefault("reverted", [])
        # Revert PR number -> the commits it undoes; they count as reverted once it merges.
        self.pending: dict[str, list[str]] = self.state.setdefault("pending_reverts", {})
        self._resume_error: Optional[str] = None
        for r in self.runs.values():
            # Cut off by a restart in the middle of a step: a person looks at it.
            if r["state"] in ("previewing", "deploying", "passing", "rolling-back"):
                self._log(r, f"the server stopped while {r['state']}")
                r["state"] = "needs-operator"
        self._save()

    def resume(self) -> Optional[str]:
        """Start the queued merge if nothing blocks it. -> the run started."""
        try:
            started = self._advance()
            self._resume_error = None
            return started
        except Exception as e:  # noqa: BLE001 - alerted once per cause; the next tick tries again
            if str(e) != self._resume_error:
                self.alert(f"fleetkit: the queued run could not start: {e}")
            self._resume_error = str(e)
            return None

    # ── bookkeeping (state is read and written under the lock) ───────────
    def _save(self) -> None:
        with self.lock:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(self.file)

    def _log(self, r: dict[str, Any], msg: str) -> None:
        r.setdefault("log", []).append({"at": self.clock(), "msg": msg})

    def _update(self, r: dict[str, Any], msg: Optional[str] = None, **fields: Any) -> None:
        with self.lock:
            r.update(fields)
            r["updated"] = self.clock()
            if msg:
                self._log(r, msg)
            self._save()

    def _set(self, r: dict[str, Any], state: str, msg: str, alert: bool = False,
             expect: Optional[tuple[str, ...]] = None) -> None:
        """Nothing changes when the run is no longer in `expect`."""
        with self.lock:
            if expect is not None and r["state"] not in expect:
                return
            self._update(r, msg, state=state)
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
        with self.lock:
            return copy.deepcopy(self.runs.get(rid))

    def list(self, state: Optional[str] = None) -> list[dict[str, Any]]:
        with self.lock:
            rs = sorted(self.runs.values(), key=lambda r: r["created"], reverse=True)
            return [copy.deepcopy(r) for r in rs if state is None or r["state"] == state]

    def _new(self, sha: str, pr: Optional[int], state: str, **fields: Any) -> dict[str, Any]:
        with self.lock:
            now = self.clock()
            r = {"id": uuid.uuid4().hex[:12], "repo": self.repo, "sha": sha, "pr": pr, "state": state,
                 "created": now, "updated": now, "log": [], **fields}
            self.runs[r["id"]] = r
            # The record stays bounded: the oldest finished runs go first.
            done = sorted((x for x in self.runs.values() if x["state"] in DONE), key=lambda x: x["created"])
            for old in done[:-KEEP]:
                del self.runs[old["id"]]
            return r

    def _green(self, shas: list[str]) -> None:
        self._remember(self.green, shas)

    def _remember(self, into: list[str], shas: list[str]) -> None:
        with self.lock:
            into.extend(s for s in shas if s not in into)
            del into[:-GREEN_KEEP]
            self._save()

    # ── trigger, guard and queue ─────────────────────────────────────────
    def trigger(self, repo: str, sha: str, source: str) -> dict[str, Any]:
        """A merge reached the deploy branch. -> the refused run, or the queue."""
        if repo.lower() != self.repo.lower():
            raise ValueError(f"this server runs {self.repo}, not {repo}")
        dup = self._duplicate(sha)
        if dup:
            return {"ignored": dup}
        try:
            why, pr = self._refusal(sha)
            why = why or self._order(sha)
        except Exception as e:
            self.alert(f"fleetkit: {source}: {sha[:12]} could not be checked, nothing queued: {e}")
            raise
        if why:
            r = self._new(sha, pr, "refused")
            self._set(r, "refused", f"{source}: {why}; nothing deploys", alert=True)
            return {"refused": why, "run": self.get(r["id"])}
        self._green([sha])
        with self.lock:
            # A revert PR merged: what it undoes, and its merge, are reverted now
            # (whatever became of its run), and its run's queue moves again.
            undone = self.pending.pop(str(pr), None)
            if undone is not None:
                self._remember(self.reverted, [*undone, sha])
            for r in self.runs.values():
                if r["state"] == "reverting" and r.get("revert_pr") == pr:
                    self._set(r, "failed", f"the revert (#{pr}) is merged")
            # Checked again under the lock: two deliveries of one merge race.
            dup = self._duplicate(sha)
            if dup:
                self._save()
                return {"ignored": dup}
            q = self.state.get("queued")
            # An older arrival never replaces a newer queued merge.
            if not (q and self.git.is_ancestor(sha, q["sha"])):
                self.state["queued"] = {"sha": sha, "pr": pr, "at": self.clock(), "source": source}
            self._save()
        return {"queued": sha, "started": self.resume()}

    def _duplicate(self, sha: str) -> Optional[str]:
        """A merge already queued or running (a redelivered webhook, the
        webhook and the workflow both) is not a new run."""
        with self.lock:
            q = self.state.get("queued")
            going = [r["sha"] for r in self.runs.values() if r["state"] in BLOCKING]
        for s in [*going, *([q["sha"]] if q else [])]:
            if sha == s or self.git.is_ancestor(sha, s):
                return f"{sha[:12]} is already queued or running (in {s[:12]})"
        return None

    def _refusal(self, sha: str) -> tuple[Optional[str], Optional[int]]:
        pr = self.forge.merged_pr(self.repo, sha)
        if not pr:
            return f"{sha[:12]} is not the merge commit of a pull request (a direct push?)", None
        n = pr.get("number")
        if (pr.get("base") or {}).get("ref") != self.branch:
            return f"#{n} was merged into {(pr.get('base') or {}).get('ref')}, not {self.branch}", n
        checks = self.forge.checks(self.repo, (pr.get("head") or {}).get("sha", ""))
        bad = [f"{c}: {checks.get(c, 'missing')}" for c in self.required if checks.get(c) not in OK_CHECK]
        if bad:
            return f"#{n} has required checks that are not green ({', '.join(bad)})", n
        return None, n

    def _order(self, sha: str, base: Optional[str] = None, retry: bool = False) -> Optional[str]:
        """Why `sha` is not a step forward: it must descend from stable, differ
        from it, and be newer than the last commit a run deployed (or be that
        very commit, when a person retries it)."""
        base = base or self.git.head(self.stable)
        if sha == base or not self.git.is_ancestor(base, sha):
            return f"{sha[:12]} is not ahead of {self.stable} {base[:12]} (an old or replayed merge?)"
        with self.lock:
            last = self.state.get("last_deployed")
        if last and not (retry and sha == last["sha"]) and self.git.is_ancestor(sha, last["sha"]):
            return f"{sha[:12]} is not newer than {last['sha'][:12]}, deployed by run {last['run']}"
        return None

    def _range_refusal(self, base: str, sha: str) -> Optional[str]:
        """Every first-parent commit of the range passed the guard (or a person accepted it)."""
        for c in self.git.first_parents(base, sha):
            with self.lock:
                if c in self.green:
                    continue
            why, _ = self._refusal(c)
            if why:
                return f"the range {base[:12]}..{sha[:12]} holds {c[:12]}: {why}"
            self._green([c])
        return None

    def _advance(self) -> Optional[str]:
        dropped = None
        with self.lock:  # one decision at a time: two callers never start two runs
            if any(r["state"] in BLOCKING for r in self.runs.values()):
                return None
            q = self.state.get("queued")
            if q is None:
                return None
            # Read before the queue is touched: a fetch that fails leaves it as it was.
            base = self.git.head(self.stable)
            del self.state["queued"]
            if q["sha"] == base or not self.git.is_ancestor(base, q["sha"]):
                self._save()
                # Stable passed it (a pass, a resolve), unless someone moved stable by hand.
                if not self.git.is_ancestor(q["sha"], base):
                    dropped = f"fleetkit: queued {q['sha'][:12]} dropped: not ahead of {self.stable} {base[:12]}"
                r = None
            else:
                r = self._new(q["sha"], q["pr"], "previewing", base=base, retry_of=q.get("retry_of"))
                self._update(r, f"range {base[:12]}..{q['sha'][:12]} ({q['source']})")
        if r is None:
            if dropped:
                self.alert(dropped)
            return None
        self.spawn(lambda: self._guarded(r, self._go))
        return r["id"]

    def _guarded(self, r: dict[str, Any], step: Callable[[dict[str, Any]], None]) -> None:
        try:
            step(r)
        except Exception as e:  # noqa: BLE001 - whatever breaks, a person looks at it
            traceback.print_exc()
            self._set(r, "needs-operator", f"{type(e).__name__}: {e}", alert=True)

    # ── a run ────────────────────────────────────────────────────────────
    def _gate(self, r: dict[str, Any], sha: str, what: str) -> Optional[str]:
        """Preview every estate at `sha`: -> why a person is needed, else None."""
        for e in self.estates:
            out = self.deployer.deploy(e, sha, preview=True, on=self.nixos_on)
            self._update(r, None, jobs={**r.get("jobs", {}), f"{what}-preview:{e}": out.get("job")})
            if out["state"] != "succeeded":
                return f"{what} preview of {e} {out['state']}: {out.get('error')}"
            changes = (out.get("result") or {}).get("nixos_changes") or {}
            if changes:
                moved = sorted(n for n, c in changes.items() if c.get("live") != c.get("built"))
                self._update(r, f"{what} preview of {e}: NixOS toplevels change on {', '.join(moved) or 'no node'}")
            hits = self.needs_operator(out.get("result") or {})
            if hits:
                return f"{what} of {e} needs a person: {'; '.join(hits)}"
        return None

    def _apply(self, r: dict[str, Any], sha: str, what: str) -> Optional[str]:
        # From the first estate on, the estates no longer all run one commit.
        self._update(r, None, applied=None)
        for e in self.estates:
            out = self.deployer.deploy(e, sha, preview=False, on=self.nixos_on)
            # What is where: each estate's outcome is in the run's log.
            with self.lock:
                if what == "deploy":
                    self.state["last_deployed"] = {"sha": sha, "run": r["id"]}
                self._update(r, f"{what} {e} at {sha[:12]}: {out['state']}",
                             jobs={**r.get("jobs", {}), f"{what}:{e}": out.get("job")})
            if out["state"] != "succeeded":
                return f"{what} of {e} {out['state']}: {out.get('error')}"
        # Every estate runs `sha` now: only then may a person call it verified.
        self._update(r, None, applied=sha)
        return None

    def _go(self, r: dict[str, Any]) -> None:
        why = (self._order(r["sha"], r["base"], retry=bool(r.get("retry_of")))
               or self._range_refusal(r["base"], r["sha"]) or self._gate(r, r["sha"], "deploy"))
        if why:
            self._set(r, "needs-operator", f"{why}; nothing deployed", True)
            return
        self._set(r, "deploying", "previews clean")
        why = self._apply(r, r["sha"], "deploy")
        if why:
            self._set(r, "needs-operator", why, True)
            return
        tickets = self.tickets(r)
        if not tickets and self.git.same_tree(r["base"], r["sha"]):
            # A merged revert and nothing else: the estates run stable's own tree again.
            self._update(r, "deployed the tree of stable itself; nothing to review", state="passing",
                         verdict={"verdict": "pass", "evidence": f"the tree is {self.stable}'s", "by": "fleetkit",
                                  "at": self.clock()})
            self._pass(r)
            return
        self._update(r, f"deployed; review of {', '.join(tickets) or 'no tickets'} requested",
                     tickets=tickets, deadline=self.clock() + self.timeout, state="reviewing")
        self._dispatch("review.requested", r)

    def needs_operator(self, result: dict[str, Any]) -> list[str]:
        """What in a preview's result stops a run: a refusal, a delete or
        replace, a change to a protected resource (Pulumi plan), a protected
        node whose toplevel changes or cannot be compared (NixOS)."""
        hits = [f"refused {x['op']} {st}/{x['key']}" for st, xs in (result.get("refused") or {}).items() for x in xs]
        if self.nixos_on and "nixos_changes" not in result:
            hits.append("the NixOS toplevels were not compared with the live ones")
        for n, c in (result.get("nixos_changes") or {}).items():
            if self.protected_node(n, c.get("tags") or []) and (not c.get("live") or c["live"] != c.get("built")):
                hits.append(f"NixOS {n}: " + (f"{c['live']} -> {c.get('built')}" if c.get("live")
                                              else "the live system could not be read") + " (protected)")
        for st, changes in (result.get("plan") or {}).items():
            for c in changes:
                name = f"{st}/{c['key']}"
                if guard.family(c["op"]) in ("delete", "replace"):
                    hits.append(f"{c['op']} {name}")
                elif c["op"] != "same" and any(fnmatch.fnmatch(name, p) for p in self.protect):
                    hits.append(f"{c['op']} {name} (protected)")
        return hits

    def protected_node(self, name: str, tags: list[str]) -> bool:
        """A Colmena node no run changes unattended: tagged mainnet, or its name
        or a tag matching a FLEETKIT_PROTECT pattern's last segment."""
        return "mainnet" in tags or any(fnmatch.fnmatch(x, p.rsplit("/", 1)[-1])
                                        for p in self.protect for x in (name, *tags))

    def tickets(self, r: dict[str, Any]) -> list[str]:
        """Keys that prefix a commit subject of the range (`KEY-1: ...`), not every
        mention, and not of commits a failed run reverted or of their revert."""
        if not self.tracker:
            return []
        pat = re.compile(rf"^({self.tracker.pattern}):")
        with self.lock:
            skip = set(self.reverted)
        return sorted({m.group(1) for c, s in self.git.log(r["base"], r["sha"])
                       if c not in skip and (m := pat.match(s))})

    def _dispatch(self, kind: str, r: dict[str, Any], **extra: Any) -> None:
        if not self.runner:
            return
        try:
            self.runner.dispatch({"event": kind, "run": r["id"], "repo": r["repo"], "url": self.url(r),
                                  "range": {"base": r.get("base"), "head": r["sha"]},
                                  "tickets": r.get("tickets", []), **extra})
        except Exception as e:  # noqa: BLE001 - recorded; /review asks again
            self._update(r, f"{kind} not dispatched: {e}")
            self.alert(f"fleetkit run {r['id']}: {kind} not dispatched: {e}")

    def _each_ticket(self, r: dict[str, Any], f: Callable[[Tracker, str], None]) -> None:
        if not self.tracker:
            return
        for t in r.get("tickets", []):
            try:
                f(self.tracker, t)
            except Exception as e:  # noqa: BLE001 - the run's outcome stands; the ticket is a person's fix
                self._update(r, f"ticket {t}: {e}")
                self.alert(f"fleetkit run {r['id']}: ticket {t}: {e}")

    # ── verdict, review, timeout, resolve ────────────────────────────────
    def verdict(self, rid: str, verdict: str, evidence: str, by: str) -> dict[str, Any]:
        with self.lock:
            r = self.runs[rid]
            if r["state"] != "reviewing":
                raise ValueError(f"run {rid} is {r['state']}, not reviewing")
            # Taken under the lock, so a second verdict (or the timeout) loses.
            self._update(r, f"verdict {verdict} by {by}",
                         verdict={"verdict": verdict, "evidence": evidence, "by": by, "at": self.clock()},
                         state="passing" if verdict == "pass" else "rolling-back")
        if verdict == "pass":
            self._guarded(r, self._pass)
        else:
            self.spawn(lambda: self._guarded(r, self._fail))
        return self.get(rid) or {}

    def _pass(self, r: dict[str, Any]) -> None:
        try:
            self.forge.fast_forward(r["repo"], self.stable, r["sha"])
        except Exception as e:  # noqa: BLE001 - stable did not move: a person decides
            self._set(r, "needs-operator", f"passed, but {self.stable} did not move: {e}", True)
            return
        note = f"Verified live by {r['verdict']['by']} ({self.url(r)}); {self.stable} is now {r['sha'][:12]}.\n\n" \
               f"{r['verdict']['evidence']}"
        self._each_ticket(r, lambda t, k: (t.comment(k, note), t.set_state(k, self.done_state)))
        self._set(r, "passed", f"{self.stable} fast-forwarded to {r['sha'][:12]}")
        self.resume()

    def _no_auto_revert(self, r: dict[str, Any]) -> list[str]:
        """Tickets that forbid an automatic revert, or might: one the tracker
        cannot read (down, or a 404, which is also what a ticket the account
        may not see answers) holds the run too."""
        held = []
        for k in r.get("tickets", []):
            try:
                t = self.tracker.get(k)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001 - unknown: a person decides
                held.append(f"{k} (unreadable: {e})")
                continue
            if t is None:
                held.append(f"{k} (not found)")
            elif "no-auto-revert" in (t.get("labels") or []):
                held.append(k)
        return held

    def _fail(self, r: dict[str, Any]) -> None:
        held = self._no_auto_revert(r) if self.tracker else []
        if held:
            self._set(r, "needs-operator", f"failed review; not reverted, no-auto-revert: {', '.join(held)}", True)
            return
        # The way back passes the same gate as the way forward.
        why = self._gate(r, r["base"], "rollback") or self._apply(r, r["base"], "rollback")
        if why:
            self._set(r, "needs-operator", f"failed review; {why}", True)
            return
        self._update(r, f"estates back at {self.stable} {r['base'][:12]}")
        name = f"fleetkit/revert-{r['id']}"
        keys = ", ".join(r.get("tickets", []))
        title = f"{keys + ': ' if keys else ''}Revert {r['base'][:10]}..{r['sha'][:10]} " \
                f"(failed live review, run {r['id']})"
        try:
            commit = self.git.revert(r["base"], r["sha"], name, title)
            pr = self.forge.open_pr(r["repo"], name, self.branch, title,
                                    f"The reviewer failed run {self.url(r)}:\n\n{r['verdict']['evidence']}")
        except Exception as e:  # noqa: BLE001 - a conflict or a refused push: a person decides
            self._set(r, "needs-operator", f"failed review; the revert could not be opened: {e}", True)
            return
        with self.lock:
            self.pending[str(pr.get("number"))] = [c for c, _ in self.git.log(r["base"], r["sha"])] + [commit]
            self._save()
        note = f"Failed live review ({self.url(r)}); the estates are back at {self.stable} {r['base'][:12]} " \
               f"and {pr.get('html_url')} reverts the change.\n\n{r['verdict']['evidence']}"
        self._update(r, f"revert #{pr.get('number')} opened; the queue waits for its merge",
                     revert_pr=pr.get("number"), revert_url=pr.get("html_url"),
                     deadline=self.clock() + self.timeout, state="reverting")
        self._each_ticket(r, lambda t, k: (t.comment(k, note), t.set_state(k, self.reopen_state),
                                           t.link(k, pr.get("html_url") or "", title)))
        self._dispatch("work.requested", r, findings=r["verdict"]["evidence"], revert=pr.get("html_url"))

    def review(self, rid: str) -> dict[str, Any]:
        """Ask for a verdict (again): a timed-out run is reviewed anew."""
        with self.lock:
            r = self.runs[rid]
            if r["state"] not in ("reviewing", "timed-out"):
                raise ValueError(f"run {rid} is {r['state']}; only a deployed run is reviewed")
            self._update(r, "review requested again", deadline=self.clock() + self.timeout, state="reviewing")
        self._dispatch("review.requested", r)
        return self.get(rid) or {}

    def tick(self) -> None:
        """Time out reviews; never revert (a reviewer outage must not undo good
        changes). Alert once about a revert left unmerged."""
        now = self.clock()
        with self.lock:
            late = [r for r in self.runs.values() if r["state"] in ("reviewing", "reverting")
                    and now > r.get("deadline", float("inf")) and not r.get("late")]
        for r in late:
            if r["state"] == "reviewing":
                self._set(r, "timed-out", "no verdict in time; the queue stops (nothing reverted)", alert=True,
                          expect=("reviewing",))
            else:
                self._update(r, "the revert is still not merged", late=True)
                self.alert(f"fleetkit run {r['id']}: revert {r.get('revert_url')} not merged; the queue waits")
        # A queued merge whose start failed (a fetch) is tried again here.
        self.resume()

    def resolve(self, rid: str, note: str, by: str, accept: bool = False, stable: bool = False) -> dict[str, Any]:
        """A person decided about a stopped run; the queue moves on. `accept`:
        the person vouches for the run's range (commits the guard refused) and
        the run's commit is queued again. `stable`: stable moves to the run's
        commit (the person verified it)."""
        with self.lock:
            r = self.runs[rid]
            if r["state"] not in ("needs-operator", "timed-out", "reverting"):
                raise ValueError(f"run {rid} is {r['state']}; nothing to resolve")
            if stable and accept:
                raise ValueError("accept runs the commit again; stable says it is verified: one or the other")
            if accept and r["state"] == "reverting":
                raise ValueError(f"run {rid} failed review and is being reverted; it is not run again")
            if stable and (r.get("verdict") or {}).get("verdict") == "fail":
                raise ValueError(f"run {rid} failed review; stable does not move to it")
            # Only a commit every estate runs (and no rollback undid) can be called verified.
            if stable and (r["state"] == "reverting" or r.get("applied") != r["sha"]):
                raise ValueError(f"run {rid}'s commit is not what the estates run; stable cannot move to it")
        if stable:
            self.forge.fast_forward(r["repo"], self.stable, r["sha"])
        if accept:
            self._green(self.git.first_parents(r["base"], r["sha"]))
            with self.lock:
                if not self.state.get("queued"):
                    self.state["queued"] = {"sha": r["sha"], "pr": r["pr"], "at": self.clock(),
                                            "source": f"accepted by {by}",
                                            "retry_of": r.get("retry_of") or r["id"]}
        self._set(r, "resolved", f"resolved by {by}{' (accepted)' if accept else ''}"
                                 f"{f', {self.stable} moved' if stable else ''}: {note}")
        self.resume()
        return self.get(rid) or {}

    def start_ticking(self, every: float = 30) -> None:
        def loop() -> None:
            while True:
                time.sleep(every)
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the loop outlives one bad tick
                    traceback.print_exc()

        threading.Thread(target=loop, name="runs-tick", daemon=True).start()
