"""The reviewer of verified runs (ADR-167 decision 5; docs/pulumi.md,
"Verified runs"): `fleetkit reviewer`, the target of a deploy server's http
runner (FLEETKIT_RUNNER=http://<this host>:<port>/). stdlib only.

On review.requested it reads the run from the deploy server with a scoped
reviewer token (runs: review): the run, its range (GET /v1/runs/{id}/diff)
and the events of its deploy jobs, lays them out in a directory, and runs an
agent there: headless Claude Code, told the plugin's review-run skill
(plugin/skills/review-run) and this site's notes. The agent answers
{verdict, evidence} (a JSON schema); this posts it.

It runs on a host of its own, never on the deploy server, with read-only
access only. An agent that fails, times out or answers nothing valid posts
no verdict: the server's review timeout stops the queue and alerts, and
nothing is reverted (a reviewer outage must not undo good changes).

  FLEETKIT_SERVER                   the deploy server's URL (required)
  FLEETKIT_REVIEWER_TOKEN(_FILE)    the scoped reviewer token (required)
  FLEETKIT_RUNNER_TOKEN(_FILE)      the bearer the deploy server sends (required)
  FLEETKIT_REVIEWER_PROMPT_FILE     the skill the agent follows (required)
  FLEETKIT_REVIEWER_NOTES_FILE      what the agent is told about this site
  FLEETKIT_REVIEWER_LISTEN          host:port (default 0.0.0.0:8741)
  FLEETKIT_REVIEWER_DIR             where runs are laid out (default ./reviews)
  FLEETKIT_REVIEWER_AGENT           the agent's executable (default claude)
  FLEETKIT_REVIEWER_MODEL           its --model (default: its own)
  FLEETKIT_REVIEWER_TOOLS           its --allowedTools (default below); nothing else runs
  FLEETKIT_REVIEWER_TIMEOUT         seconds an agent may take (default 1200)
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .settings import _read_secret

# Read and diagnose: files of the run, and ssh, curl and jq for the live
# fleet. Anything else is refused (--permission-mode dontAsk).
TOOLS = "Read Glob Grep Bash(ssh:*) Bash(curl:*) Bash(jq:*)"
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"enum": ["pass", "fail"]}, "evidence": {"type": "string", "minLength": 1}},
    "required": ["verdict", "evidence"],
}
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def log(msg: str) -> None:
    print(f"fleetkit-reviewer: {msg}", flush=True)


class Server:
    """The deploy server, as the reviewer token sees it."""

    def __init__(self, url: str, token: str):
        self.url, self.h = url.rstrip("/"), {"Authorization": f"Bearer {token}", "User-Agent": "fleetkit-reviewer"}

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.url + path, data=data, method=method,
                                   headers={**self.h, "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read() or b"null")


def verdict_of(stdout: str) -> Optional[dict[str, str]]:
    """The agent's answer (claude -p --output-format json): its structured
    output, else a JSON object in its last message. None unless it is a
    pass or a fail with evidence."""
    try:
        out = json.loads(stdout)
    except ValueError:
        return None
    v = out.get("structured_output") if isinstance(out, dict) else None
    if not isinstance(v, dict) and isinstance(out, dict) and isinstance(out.get("result"), str):
        m = re.search(r"\{.*\}", out["result"], re.S)
        try:
            v = json.loads(m.group(0)) if m else None
        except ValueError:
            v = None
    if (isinstance(v, dict) and v.get("verdict") in ("pass", "fail")
            and isinstance(v.get("evidence"), str) and v["evidence"].strip()):
        return {"verdict": v["verdict"], "evidence": v["evidence"].strip()}
    return None


class Reviewer:
    def __init__(self, server: Server, root: Path, argv: list[str], timeout: float):
        self.server, self.root, self.argv, self.timeout = server, root, argv, timeout
        self.q: queue.Queue[str] = queue.Queue()
        self.pending: set[str] = set()
        self.lock = threading.Lock()

    def offer(self, rid: str) -> bool:
        """Queue a review of `rid` unless one is already queued or running."""
        with self.lock:
            if rid in self.pending:
                return False
            self.pending.add(rid)
        self.q.put(rid)
        return True

    def work(self) -> None:
        while True:
            rid = self.q.get()
            try:
                self.review(rid)
            except Exception:  # noqa: BLE001 - logged; the server's timeout covers it
                traceback.print_exc()
            finally:
                with self.lock:
                    self.pending.discard(rid)

    def layout(self, rid: str, run: dict[str, Any]) -> Path:
        d = self.root / rid
        (d / "jobs").mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps(run, indent=1))
        ch = self.server.call("GET", f"/v1/runs/{rid}/diff")
        (d / "diff.patch").write_text(ch.pop("diff", ""))
        (d / "changes.json").write_text(json.dumps(ch, indent=1))
        for name, jid in (run.get("jobs") or {}).items():
            if not jid or not RUN_ID.match(str(jid)):
                continue
            try:
                job = self.server.call("GET", f"/v1/deploys/{jid}")
                job["events"] = self.server.call("GET", f"/v1/deploys/{jid}/events?after=0").get("events", [])
            except urllib.error.HTTPError as e:  # another estate's job is 404 to this token
                job = {"error": f"{e.code}"}
            (d / "jobs" / f"{name.replace(':', '_').replace('/', '_')}.json").write_text(json.dumps(job, indent=1))
        return d

    def review(self, rid: str) -> None:
        run = self.server.call("GET", f"/v1/runs/{rid}")
        if run.get("state") != "reviewing":
            log(f"run {rid} is {run.get('state')}; not reviewed")
            return
        d = self.layout(rid, run)
        prompt = (f"Review fleetkit run {rid} ({run.get('repo')} {str(run.get('base'))[:12]}.."
                  f"{run['sha'][:12]}) as the review-run instructions say. Its files are in {d}: run.json, "
                  "changes.json (commits, tickets), diff.patch, jobs/*.json (the deploy jobs' events).")
        log(f"run {rid}: agent started")
        t0 = time.time()
        try:
            p = subprocess.run([*self.argv, prompt], cwd=d, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            log(f"run {rid}: agent timed out after {self.timeout:.0f}s; no verdict")
            return
        (d / "agent.json").write_text(p.stdout)
        v = verdict_of(p.stdout) if p.returncode == 0 else None
        if v is None:
            log(f"run {rid}: agent exit {p.returncode} after {time.time() - t0:.0f}s, no valid verdict; "
                f"none posted. {p.stderr[-500:]}")
            return
        # The run may have moved on meanwhile (a person resolved it, a newer request).
        now = self.server.call("GET", f"/v1/runs/{rid}")
        if now.get("state") != "reviewing" or now.get("sha") != run["sha"]:
            log(f"run {rid} is {now.get('state')} now; verdict {v['verdict']} not posted")
            return
        try:
            self.server.call("POST", f"/v1/runs/{rid}/verdict", v)
        except urllib.error.HTTPError as e:
            log(f"run {rid}: verdict {v['verdict']} refused: {e.code} {e.read()[:300]!r}")
            return
        log(f"run {rid}: verdict {v['verdict']} posted after {time.time() - t0:.0f}s")


def handler(rv: Reviewer, token: str) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def reply(self, code: int, body: dict[str, Any]) -> None:
            out = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self) -> None:  # noqa: N802
            self.reply(200, {"ok": True}) if self.path == "/healthz" else self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self.reply(401, {"error": "bad token"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                return self.reply(413, {"error": "too large"})
            try:
                ev = json.loads(self.rfile.read(n))
            except ValueError:
                return self.reply(400, {"error": "not JSON"})
            if not isinstance(ev, dict) or ev.get("event") != "review.requested":
                return self.reply(202, {"ignored": ev.get("event") if isinstance(ev, dict) else None})
            rid = str(ev.get("run") or "")
            if not RUN_ID.match(rid):
                return self.reply(400, {"error": "no run id"})
            self.reply(202, {"queued": rv.offer(rid), "run": rid})

        def log_message(self, fmt: str, *args: Any) -> None:
            log(fmt % args)

    return H


def main() -> None:
    env = os.environ.get
    server, rtoken, btoken = env("FLEETKIT_SERVER"), _read_secret("FLEETKIT_REVIEWER_TOKEN"), \
        _read_secret("FLEETKIT_RUNNER_TOKEN")
    prompt = env("FLEETKIT_REVIEWER_PROMPT_FILE")
    if not (server and rtoken and btoken and prompt):
        raise ValueError("fleetkit reviewer needs FLEETKIT_SERVER, FLEETKIT_REVIEWER_TOKEN(_FILE), "
                         "FLEETKIT_RUNNER_TOKEN(_FILE) and FLEETKIT_REVIEWER_PROMPT_FILE")
    argv = [env("FLEETKIT_REVIEWER_AGENT") or "claude", "-p", "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA), "--permission-mode", "dontAsk",
            "--allowedTools", env("FLEETKIT_REVIEWER_TOOLS") or TOOLS,
            "--append-system-prompt", Path(prompt).read_text()
            + (Path(env("FLEETKIT_REVIEWER_NOTES_FILE", "")).read_text() if env("FLEETKIT_REVIEWER_NOTES_FILE") else "")]
    if env("FLEETKIT_REVIEWER_MODEL"):
        argv += ["--model", env("FLEETKIT_REVIEWER_MODEL", "")]
    rv = Reviewer(Server(server, rtoken), Path(env("FLEETKIT_REVIEWER_DIR") or "reviews").resolve(), argv,
                  float(env("FLEETKIT_REVIEWER_TIMEOUT") or 1200))
    threading.Thread(target=rv.work, name="review", daemon=True).start()
    host, _, port = (env("FLEETKIT_REVIEWER_LISTEN") or "0.0.0.0:8741").rpartition(":")
    log(f"listening on {host}:{port}, deploy server {server}")
    ThreadingHTTPServer((host, int(port)), handler(rv, btoken)).serve_forever()
