"""Reporting deploys back to GitHub: a commit status per estate
(context fleetkit/<estate>) and one comment per estate on the pull request,
edited in place as the PR moves. Needs a token that may write statuses and
issue comments on the repo. stdlib only.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any


class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com"):
        self.token, self.api = token, api.rstrip("/")

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(f"{self.api}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
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
