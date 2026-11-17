"""Reporting deploys back to GitHub: a commit status per estate
(context fleetkit/<estate>) and one comment per estate on the pull request,
edited in place as the PR moves. Needs a token that may write statuses and
issue comments on the repo. stdlib only.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com", token_file: str | None = None):
        self.token, self.api, self.token_file = token, api.rstrip("/"), token_file

    def _token(self) -> str:
        if self.token_file:
            try:
                return open(self.token_file).read().strip() or self.token
            except OSError:
                return self.token  # the value read at start; GitHub says if it has expired
        return self.token

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(f"{self.api}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "fleetkit",
        })
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {path}: {e.code} {e.read()[:300]!r}")
        return json.loads(raw) if raw else None

    def status(self, repo: str, sha: str, state: str, context: str, description: str,
               url: str | None = None) -> None:
        """state: pending | success | failure | error."""
        body = {"state": state, "context": context, "description": description[:139]}
        if url:
            body["target_url"] = url
        self._req("POST", f"/repos/{repo}/statuses/{sha}", body)

    def comment(self, repo: str, pr: int, marker: str, body: str) -> None:
        """Create the PR's comment carrying `marker`, or edit it in place."""
        tag = f"<!-- {marker} -->"
        text = f"{tag}\n{body}"
        page = 1
        while True:
            comments = self._req("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}") or []
            for c in comments:
                if tag in (c.get("body") or ""):
                    self._req("PATCH", f"/repos/{repo}/issues/comments/{c['id']}", {"body": text})
                    return
            if len(comments) < 100:
                break
            page += 1
        self._req("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": text})

    # ── verified runs (runs.py: the Forge adapter) ───────────────────────
    def merged_pr(self, repo: str, sha: str) -> dict[str, Any] | None:
        """The merged pull request whose merge commit is `sha`, else None.
        GitHub lists merged pull requests for a commit only once it is on the
        default branch: the deploy branch must be the repository's default.
        Right after a merge the link can lag: asked again a few times."""
        for wait in (2, 5, 10, None):
            for pr in self._req("GET", f"/repos/{repo}/commits/{sha}/pulls") or []:
                if pr.get("merged_at") and pr.get("merge_commit_sha") == sha:
                    return pr
            if wait:
                time.sleep(wait)
        return None

    def checks(self, repo: str, sha: str) -> dict[str, str]:
        """Check-run name (a job's own name, not "workflow / job") or status
        context -> its outcome on `sha`. Per app the newest run of a name
        counts; when apps disagree the worst wins, so a later green run
        elsewhere does not hide a red one."""
        out: dict[str, str] = {}
        # The combined status holds the newest status of each context.
        st = self._req("GET", f"/repos/{repo}/commits/{sha}/status?per_page=100") or {}
        for s in st.get("statuses") or []:
            out[s["context"]] = s["state"]
        newest: dict[tuple[str, str], dict[str, Any]] = {}
        page = 1
        while True:
            got = (self._req("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}") or {})
            runs = got.get("check_runs") or []
            for c in runs:
                k = ((c.get("app") or {}).get("slug", ""), c["name"])
                if k not in newest or c["id"] > newest[k]["id"]:
                    newest[k] = c
            if len(runs) < 100:
                break
            page += 1
        ok = ("success", "neutral", "skipped")
        for (_, name), c in sorted(newest.items()):
            v = c.get("conclusion") or c.get("status") or "pending"
            if out.get(name) in ok or name not in out:
                out[name] = v
        return out

    def fast_forward(self, repo: str, branch: str, sha: str) -> None:
        """Move `branch` to `sha`; GitHub refuses (422) unless it is a fast-forward."""
        self._req("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": False})

    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return self._req("POST", f"/repos/{repo}/pulls", {"head": head, "base": base, "title": title, "body": body})
