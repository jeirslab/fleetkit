"""GitOps for the deploy server: the estate repo's branch is the desired state.

The server keeps a bare mirror of the repo (<state>/repo.git). A deploy names a
commit (or takes the branch head), and runs from a checkout of exactly that
commit (<state>/checkouts/<sha>, a shared clone, detached): Nix evaluates a
clean tree at a known revision, and the job records the sha.

A push reaches the server either way:
  pull   FLEETKIT_POLL=<seconds>: fetch, and deploy each FLEETKIT_DEPLOY_ON_PUSH
         estate whose last submitted commit is not the head;
  push   POST /v1/hooks/github (a push to the branch, HMAC-signed with
         FLEETKIT_WEBHOOK_SECRET): the same, at once.
FLEETKIT_PUSH_MODE=preview plans every push instead (pulumi preview, colmena
build) and leaves applying to a POST /v1/deploys with the rev. An estate that
is still deploying is skipped and picked up by the next poll or
push. A commit is submitted once per estate: a failed deploy is not retried on
its own (fix forward with a commit, or POST /v1/deploys with the rev).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .jobs import BusyError, JobManager
from .pipeline import DeployRequest
from .settings import GitSettings, Settings


class GitError(Exception):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip()[-1000:]}")
    return p.stdout.strip()


class Repo:
    def __init__(self, s: Settings):
        assert s.git is not None
        self.g: GitSettings = s.git
        self.mirror = s.state_dir / "repo.git"
        self.checkouts = s.state_dir / "checkouts"
        self.lock = threading.Lock()

    def fetch(self) -> str:
        """Update the mirror; -> the branch head."""
        with self.lock:
            if not (self.mirror / "HEAD").exists():
                self.mirror.parent.mkdir(parents=True, exist_ok=True)
                _git("clone", "--mirror", self.g.url, str(self.mirror))
            else:
                _git("--git-dir", str(self.mirror), "fetch", "--prune", "origin")
            return _git("--git-dir", str(self.mirror), "rev-parse", f"refs/heads/{self.g.branch}")

    def resolve(self, rev: str | None) -> str:
        head = self.fetch()
        if rev is None:
            return head
        try:
            return _git("--git-dir", str(self.mirror), "rev-parse", "--verify", f"{rev}^{{commit}}")
        except GitError:
            raise GitError(f"no commit {rev!r} in {self.g.url}")

    def checkout(self, sha: str) -> Path:
        with self.lock:
            d = self.checkouts / sha
            if not (d / "flake.nix").exists():
                if d.exists():
                    shutil.rmtree(d)
                self.checkouts.mkdir(parents=True, exist_ok=True)
                _git("clone", "--quiet", "--shared", "--no-checkout", str(self.mirror), str(d))
                _git("checkout", "--quiet", "--detach", sha, cwd=d)
            d.touch()
            self._prune(keep={sha})
            return d

    def _prune(self, keep: set[str]) -> None:
        olds = sorted((p for p in self.checkouts.iterdir() if p.is_dir() and p.name not in keep),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[self.g.keep - 1:]:
            shutil.rmtree(p, ignore_errors=True)


class GitOps:
    """Push and poll triggers, and what each estate last had submitted."""

    def __init__(self, s: Settings, repo: Repo, manager: JobManager):
        assert s.git is not None
        self.s, self.g, self.repo, self.manager = s, s.git, repo, manager
        self.file = s.state_dir / "gitops.json"
        self.state: dict[str, Any] = json.loads(self.file.read_text()) if self.file.exists() else {}
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def _save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.file)

    def sync(self, reason: str, head: str | None = None) -> dict[str, Any]:
        """Deploy every push-deployed estate whose last submitted commit is not
        the branch head."""
        head = self.repo.resolve(head)
        out: dict[str, Any] = {"head": head, "reason": reason, "submitted": {}, "skipped": {}}
        with self.lock:
            last = self.state.setdefault("submitted", {})
            for estate in self.g.deploy_on_push:
                if last.get(estate, {}).get("rev") == head:
                    out["skipped"][estate] = "already submitted"
                    continue
                try:
                    j = self.manager.submit(DeployRequest(
                        estate=estate, rev=head, preview=self.g.push_mode == "preview"))
                except BusyError as e:
                    out["skipped"][estate] = f"deploying (job {e}); next poll or push"
                    continue
                last[estate] = {"rev": head, "job": j.id, "at": time.time(), "reason": reason}
                out["submitted"][estate] = j.id
            self.state["head"] = head
            self._save()
        return out

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"repo": self.g.url, "branch": self.g.branch, "poll": self.g.poll, "push_mode": self.g.push_mode,
                    "deploy_on_push": self.g.deploy_on_push, **self.state}

    def start_polling(self) -> None:
        if self.g.poll <= 0:
            return

        def loop() -> None:
            while not self._stop.wait(self.g.poll):
                try:
                    self.sync("poll")
                except Exception as e:  # noqa: BLE001 - the loop outlives one bad fetch
                    with self.lock:
                        self.state["last_error"] = {"at": time.time(), "error": str(e)}
                        self._save()

        threading.Thread(target=loop, name="gitops-poll", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
