"""GitOps for the deploy server: the estate repo's branch is the desired state.

The server keeps a bare mirror of the repo (<state>/repo.git). A deploy names a
commit (or takes the branch head), and runs from a checkout of exactly that
commit (<state>/checkouts/<sha>, a shared clone, detached): Nix evaluates a
clean tree at a known revision, and the job records the sha.

Pull requests (POST /v1/hooks/github, event pull_request): opening or pushing
to a PR into the branch previews each estate at the PR's head commit and
reports back on the PR (a commit status per estate, one comment per estate
edited in place; FLEETKIT_GITHUB_TOKEN). Only a PR from a branch of the same
repo, by an owner, member or collaborator, is previewed: a preview runs the
PR's program with the estate's decrypted credentials, and a PR can point a
provider at any endpoint. With FLEETKIT_TRIGGER=pr, merging is what deploys
(the merge commit, reported on the PR) and pushes and polls deploy nothing.

A push reaches the server either way (FLEETKIT_TRIGGER=push, the default):
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

from .github import GitHub, GitHubError
from .jobs import BusyError, Job, JobManager
from .pipeline import DeployRequest
from .settings import GitSettings, Settings

TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")
PREVIEW_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review")


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
        self.gh = GitHub(self.g.github_token, self.g.github_api) if self.g.github_token else None
        manager.on_finish.append(self._finished)

    def _save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.file)

    def sync(self, reason: str, head: str | None = None) -> dict[str, Any]:
        """Deploy every push-deployed estate whose last submitted commit is not
        the branch head."""
        if self.g.trigger == "pr":
            return {"ignored": "FLEETKIT_TRIGGER=pr: merged pull requests deploy, not pushes"}
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

    # ── pull requests ────────────────────────────────────────────────────
    def _submit(self, req: DeployRequest, link: dict[str, Any]) -> str:
        """Submit, or queue behind the estate's running job: a PR must not lose
        its preview because the estate was busy. -> job id or "queued"."""
        try:
            j = self.manager.submit(req)
        except BusyError:
            with self.lock:
                self.state.setdefault("pending", []).append({"request": req.model_dump(), "link": link})
                self._save()
            return "queued"
        with self.lock:
            self.state.setdefault("links", {})[j.id] = link
            self._save()
        self._report_start(j.id, link)
        return j.id

    def on_pull_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        pr = payload.get("pull_request") or {}
        repo = (payload.get("repository") or {}).get("full_name", "")
        number = pr.get("number")
        if (pr.get("base") or {}).get("ref") != self.g.branch:
            return {"ignored": f"base {(pr.get('base') or {}).get('ref')}"}
        merged = action == "closed" and pr.get("merged")
        if action not in PREVIEW_ACTIONS and not merged:
            return {"ignored": f"action {action}"}
        if merged and self.g.trigger != "pr":
            return {"ignored": "merged: FLEETKIT_TRIGGER=push deploys the push"}
        if not merged and pr.get("draft"):
            return {"ignored": "draft"}
        head = pr.get("head") or {}
        same_repo = (head.get("repo") or {}).get("full_name") == repo
        trusted = pr.get("author_association") in TRUSTED
        sha = pr.get("merge_commit_sha") if merged else head.get("sha")
        if not merged and not (same_repo and trusted):
            why = "from a fork" if not same_repo else f"author is {pr.get('author_association')}"
            self._status(repo, sha, "error", None, f"not previewed: {why}", None)
            return {"ignored": f"untrusted pull request ({why})"}
        sha = self.repo.resolve(sha)
        kind = "deploy" if merged else "preview"
        out: dict[str, Any] = {"pr": number, "sha": sha, "kind": kind, "submitted": {}}
        for estate in self.g.deploy_on_push:
            req = DeployRequest(estate=estate, rev=sha, pr=number,
                                preview=(not merged) or self.g.push_mode == "preview")
            link = {"repo": repo, "pr": number, "sha": sha, "estate": estate, "kind": kind}
            out["submitted"][estate] = self._submit(req, link)
        return out

    def _url(self, jid: str) -> str | None:
        return f"{self.g.public_url.rstrip('/')}/v1/deploys/{jid}" if self.g.public_url else None

    def _status(self, repo: str, sha: str | None, state: str, estate: str | None, desc: str,
                url: str | None) -> None:
        if not self.gh or not sha:
            return
        ctx = f"fleetkit/{estate}" if estate else "fleetkit"
        try:
            self.gh.status(repo, sha, state, ctx, desc, url)
        except GitHubError as e:
            self._error(e)

    def _error(self, e: Exception) -> None:
        with self.lock:
            self.state["last_error"] = {"at": time.time(), "error": str(e)}
            self._save()

    def _report_start(self, jid: str, link: dict[str, Any]) -> None:
        verb = "previewing" if link["kind"] == "preview" else "deploying"
        self._status(link["repo"], link["sha"], "pending", link["estate"], f"{verb} (job {jid})", self._url(jid))

    def _finished(self, j: Job) -> None:
        with self.lock:
            link = self.state.get("links", {}).pop(j.id, None)
            if link is not None:
                self._save()
        if link is not None:
            self._report_end(j, link)
        self._drain(j.request.estate)

    def _drain(self, estate: str) -> None:
        with self.lock:
            pending = self.state.get("pending", [])
            nxt = next((p for p in pending if p["request"]["estate"] == estate), None)
            if nxt is None:
                return
            pending.remove(nxt)
            self._save()
        self._submit(DeployRequest(**nxt["request"]), nxt["link"])

    def _report_end(self, j: Job, link: dict[str, Any]) -> None:
        ok = j.state == "succeeded"
        verb = "preview" if link["kind"] == "preview" else "deploy"
        changes = ((j.result or {}).get("infra") or {}) if ok else {}
        summary = ", ".join(f"{st}: {_ops(c)}" for st, c in changes.items()) or j.state
        self._status(link["repo"], link["sha"], "success" if ok else "failure", link["estate"],
                     f"{verb} {j.state}: {summary}", self._url(j.id))
        if not self.gh:
            return
        try:
            self.gh.comment(link["repo"], link["pr"], f"fleetkit:{link['estate']}",
                            _comment(j, link, self._url(j.id), merge_deploys=self.g.trigger == "pr"))
        except GitHubError as e:
            self._error(e)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {"repo": self.g.url, "branch": self.g.branch, "poll": self.g.poll, "push_mode": self.g.push_mode,
                    "trigger": self.g.trigger,
                    "deploy_on_push": self.g.deploy_on_push, **self.state}

    def start_polling(self) -> None:
        if self.g.poll <= 0 or self.g.trigger == "pr":
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


OPS = ("create", "update", "replace", "delete", "same")


def _ops(c: dict[str, int]) -> str:
    changed = " ".join(f"{op} {n}" for op, n in sorted(c.items()) if op != "same" and n)
    return changed or "no changes"


def _comment(j: Job, link: dict[str, Any], url: str | None, merge_deploys: bool) -> str:
    """The PR comment for one estate's job."""
    ok = j.state == "succeeded"
    verb = "Preview" if link["kind"] == "preview" else "Deploy"
    head = f"### {'✅' if ok else '❌'} fleetkit {verb.lower()}: `{link['estate']}` at `{link['sha'][:10]}` ({j.state})"
    lines = [head, ""]
    result = j.result or {}
    infra = result.get("infra") or {}
    if infra:
        lines += ["| stack | " + " | ".join(OPS) + " | program |", "|---" * (len(OPS) + 2) + "|"]
        for st, c in sorted(infra.items()):
            prog = (result.get("programs") or {}).get(st, "")
            lines.append(f"| `{st}` | " + " | ".join(str(c.get(op, 0)) for op in OPS)
                         + f" | `{prog.rsplit('/', 1)[-1]}` |")
        lines.append("")
    if result.get("nixos"):
        lines.append(f"NixOS: {'built' if result['nixos'] == 'built' else 'colmena apply ' + result['nixos']}"
                     f" (`hives.{j.request.hive or j.request.estate}`)")
    if j.error:
        lines += ["", "```", j.error[-1500:], "```"]
    if url:
        lines += ["", f"[job {j.id}]({url}) · events: `{url}/events`"]
    else:
        lines += ["", f"job `{j.id}`"]
    if link["kind"] == "preview" and ok and merge_deploys:
        lines += ["", "_Merging deploys this._"]
    return "\n".join(lines)
