"""The real adapters of verified runs (runs.py), and the pipeline built from
the environment. stdlib only.

  FLEETKIT_STABLE_BRANCH      the last verified commit (default stable)
  FLEETKIT_REQUIRED_CHECKS    check names a merged PR's head must have green (comma-separated)
  FLEETKIT_PROTECT            fnmatch patterns of <stack>/<key>: any change stops the run
  FLEETKIT_REVIEW_TIMEOUT     seconds a verdict may take (default 1800)
  FLEETKIT_RUNNER             exec:<command> (the event as JSON on stdin) or http(s)://<url> (POSTed)
  FLEETKIT_RUNNER_TOKEN(_FILE)  bearer token for an http runner
  FLEETKIT_TRACKER            jira (or unset: no tickets)
  FLEETKIT_JIRA_URL, FLEETKIT_JIRA_USER, FLEETKIT_JIRA_TOKEN(_FILE)
  FLEETKIT_TICKET_PATTERN     regex of a ticket key (default [A-Z][A-Z0-9]+-[0-9]+)
  FLEETKIT_TICKET_DONE, FLEETKIT_TICKET_REOPEN   state names (default Done, To Do)
  FLEETKIT_NTFY_URL, FLEETKIT_NTFY_TOKEN(_FILE)  where alerts go (unset: the run's log only)
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import threading
import time
import urllib.request
from typing import Any

from .jobs import TERMINAL, BusyError, JobManager
from .pipeline import DeployRequest
from .settings import Settings, _read_secret


def _http(method: str, url: str, headers: dict[str, str], body: Any = None, raw: bytes | None = None) -> Any:
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json", "User-Agent": "fleetkit", **headers})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            out = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url}: {e.code} {e.read()[:300]!r}")
    return json.loads(out) if out and out[:1] in (b"{", b"[") else None


class Jira:
    """Tracker: Jira Cloud REST v3, basic auth with an API token."""

    def __init__(self, url: str, user: str, token: str, pattern: str):
        self.api = url.rstrip("/") + "/rest/api/3"
        self.pattern = pattern
        self.h = {"Authorization": "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()}

    def get(self, key: str) -> dict[str, Any]:
        f = _http("GET", f"{self.api}/issue/{key}?fields=summary,status,labels", self.h)["fields"]
        return {"key": key, "summary": f["summary"], "state": f["status"]["name"], "labels": f.get("labels") or []}

    def comment(self, key: str, text: str) -> None:
        paras = [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in text.split("\n\n") if p]
        _http("POST", f"{self.api}/issue/{key}/comment", self.h, {"body": {"type": "doc", "version": 1,
                                                                            "content": paras}})

    def set_state(self, key: str, state: str) -> None:
        ts = _http("GET", f"{self.api}/issue/{key}/transitions", self.h)["transitions"]
        t = next((t for t in ts if t["to"]["name"].lower() == state.lower()), None)
        if t is None:
            raise RuntimeError(f"{key} has no transition to {state!r}")
        _http("POST", f"{self.api}/issue/{key}/transitions", self.h, {"transition": {"id": t["id"]}})

    def link(self, key: str, url: str, title: str) -> None:
        _http("POST", f"{self.api}/issue/{key}/remotelink", self.h, {"object": {"url": url, "title": title}})


class ExecRunner:
    """Runner: start a command with the event as JSON on stdin; do not wait for it."""

    def __init__(self, command: str):
        self.argv = shlex.split(command)

    def dispatch(self, event: dict[str, Any]) -> None:
        p = subprocess.Popen(self.argv, stdin=subprocess.PIPE)
        assert p.stdin is not None
        p.stdin.write(json.dumps(event).encode())
        p.stdin.close()
        threading.Thread(target=p.wait, daemon=True).start()  # reaped when it ends


class HttpRunner:
    """Runner: POST the event as JSON (a CLI proxy, a T3 Code endpoint, anything)."""

    def __init__(self, url: str, token: str | None = None):
        self.url, self.h = url, ({"Authorization": f"Bearer {token}"} if token else {})

    def dispatch(self, event: dict[str, Any]) -> None:
        _http("POST", self.url, self.h, event)


class JobDeployer:
    """Deployer: a job on the server's JobManager, waited for."""

    def __init__(self, manager: JobManager, wait: float = 5):
        self.manager, self.wait = manager, wait

    def deploy(self, estate: str, sha: str, preview: bool) -> dict[str, Any]:
        while True:
            try:
                j = self.manager.submit(DeployRequest(estate=estate, rev=sha, preview=preview), by="pipeline")
                break
            except BusyError:  # someone else's job on the estate: wait for it
                busy = self.manager.jobs.get(self.manager.active.get(estate, ""))
                if busy is not None:
                    busy.wait_events(len(busy.events), self.wait)
                else:  # it ended between the two looks
                    time.sleep(0.1)
        while j.state not in TERMINAL:
            j.wait_events(len(j.events), self.wait)
        return {"state": j.state, "job": j.id, "result": j.result, "error": j.error}


def ntfy(url: str, token: str | None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return lambda msg: _http("POST", url, {**h, "Title": "fleetkit", "Tags": "warning"}, raw=msg.encode())


def pipeline(s: Settings, repo: Any, manager: JobManager) -> Any:
    """The Pipeline of a server with FLEETKIT_TRIGGER=verified."""
    from .github import GitHub
    from .runs import Pipeline

    g = s.git
    assert g is not None
    if not g.github_token:
        raise ValueError("FLEETKIT_TRIGGER=verified needs FLEETKIT_GITHUB_TOKEN(_FILE): the merge guard asks GitHub")
    env = os.environ.get
    split = lambda v: tuple(x for x in (env(v) or "").split(",") if x)  # noqa: E731
    if not split("FLEETKIT_REQUIRED_CHECKS"):
        raise ValueError("FLEETKIT_TRIGGER=verified needs FLEETKIT_REQUIRED_CHECKS: a merge deploys only when they "
                         "are green")
    tracker = None
    if env("FLEETKIT_TRACKER") == "jira":
        if not (env("FLEETKIT_JIRA_URL") and env("FLEETKIT_JIRA_USER") and _read_secret("FLEETKIT_JIRA_TOKEN")):
            raise ValueError("FLEETKIT_TRACKER=jira needs FLEETKIT_JIRA_URL, FLEETKIT_JIRA_USER and "
                             "FLEETKIT_JIRA_TOKEN(_FILE)")
        tracker = Jira(env("FLEETKIT_JIRA_URL", ""), env("FLEETKIT_JIRA_USER", ""),
                       _read_secret("FLEETKIT_JIRA_TOKEN") or "",
                       env("FLEETKIT_TICKET_PATTERN") or r"[A-Z][A-Z0-9]+-[0-9]+")
    elif env("FLEETKIT_TRACKER"):
        raise ValueError(f"FLEETKIT_TRACKER is {env('FLEETKIT_TRACKER')!r}; jira or unset")
    spec = env("FLEETKIT_RUNNER") or ""
    runner: Any = None
    if spec.startswith("exec:"):
        runner = ExecRunner(spec[5:])
    elif spec.startswith(("http://", "https://")):
        runner = HttpRunner(spec, _read_secret("FLEETKIT_RUNNER_TOKEN"))
    elif spec:
        raise ValueError(f"FLEETKIT_RUNNER is {spec!r}; exec:<command> or an http(s) URL")
    alert = ntfy(env("FLEETKIT_NTFY_URL", ""), _read_secret("FLEETKIT_NTFY_TOKEN")) if env("FLEETKIT_NTFY_URL") \
        else (lambda msg: None)
    return Pipeline(s.state_dir, g.branch, g.deploy_on_push, GitHub(g.github_token, g.github_api), tracker, runner,
                    JobDeployer(manager), repo, stable=env("FLEETKIT_STABLE_BRANCH") or "stable",
                    required=split("FLEETKIT_REQUIRED_CHECKS"), protect=split("FLEETKIT_PROTECT"),
                    timeout=float(env("FLEETKIT_REVIEW_TIMEOUT") or 1800),
                    done_state=env("FLEETKIT_TICKET_DONE") or "Done",
                    reopen_state=env("FLEETKIT_TICKET_REOPEN") or "To Do", public_url=g.public_url, alert=alert)
