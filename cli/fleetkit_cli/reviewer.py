"""The reviewer of verified runs (ADR-167 decision 5; docs/pulumi.md,
"Verified runs"). stdlib only. Two processes under two users, sharing a
spool directory, so the agent never runs where the tokens are:

`fleetkit reviewer` (the service; holds the runner and reviewer tokens) is
the target of a deploy server's http runner (FLEETKIT_RUNNER=http://<host>:
<port>/). On review.requested it reads the run with the reviewer's scoped
token (runs: review): the run, its range (GET /v1/runs/{id}/diff) and the
events of its deploy jobs, lays them out in <spool>/runs/<id>/, queues
<spool>/queue/<id> and waits for the agent's answer, which it posts.

`fleetkit reviewer --agent` (the agent; holds the model's credential only)
takes queued runs one at a time and runs headless Claude Code in the run's
directory, told the plugin's review-run skill and the site's notes, with the
built-in tools Bash, Read, Grep and Glob, Bash allowed only for the site's
read-only wrappers (FLEETKIT_REVIEWER_COMMANDS, e.g. fleet-ssh and
fleet-get), no settings, no MCP servers and a fresh config directory per run.
It writes claude's JSON answer to agent.json, then `done`.

Only claude's structured output (the JSON schema: pass or fail, evidence)
is a verdict, and only when claude reports no error. An agent that fails,
times out or answers nothing valid posts nothing: the server's review
timeout stops the queue and alerts, and nothing is reverted (a reviewer
outage must not undo good changes).

  service: FLEETKIT_SERVER, FLEETKIT_REVIEWER_TOKEN(_FILE), FLEETKIT_RUNNER_TOKEN(_FILE) (required)
  agent:   FLEETKIT_REVIEWER_PROMPT_FILE (required), FLEETKIT_REVIEWER_NOTES_FILE,
           FLEETKIT_REVIEWER_COMMANDS (space-separated; default fleet-ssh fleet-get),
           FLEETKIT_REVIEWER_AGENT (default claude), FLEETKIT_REVIEWER_MODEL,
           ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN
  both:    FLEETKIT_REVIEWER_SPOOL (default ./spool), FLEETKIT_REVIEWER_TIMEOUT
           (seconds an agent may take, default 1200), FLEETKIT_REVIEWER_LISTEN
           (service, host:port, default 0.0.0.0:8741)
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .settings import _read_secret

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"enum": ["pass", "fail"]}, "evidence": {"type": "string", "minLength": 1}},
    "required": ["verdict", "evidence"],
}
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# What the agent's process gets of the environment: no token, no path to one.
AGENT_ENV = ("PATH", "LANG", "TZ", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER",
             "DISABLE_TELEMETRY")


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
    """The verdict in claude's answer (-p --output-format json --json-schema):
    its structured output, when claude reports success and no error. None
    otherwise: text in its messages is never a verdict."""
    try:
        out = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(out, dict) or out.get("is_error") is not False or out.get("subtype") != "success":
        return None
    v = out.get("structured_output")
    if (isinstance(v, dict) and v.get("verdict") in ("pass", "fail")
            and isinstance(v.get("evidence"), str) and v["evidence"].strip()):
        return {"verdict": v["verdict"], "evidence": v["evidence"].strip()}
    return None


# ── the agent's side ─────────────────────────────────────────────────────
def agent_argv(prompt_file: str, notes_file: Optional[str], commands: list[str], exe: str = "claude",
               model: Optional[str] = None) -> list[str]:
    system = Path(prompt_file).read_text() + (Path(notes_file).read_text() if notes_file else "")
    argv = [exe, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
            "--permission-mode", "dontAsk", "--tools", "Bash,Read,Grep,Glob",
            "--allowedTools", " ".join(f"Bash({c}:*)" for c in commands),
            "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
            "--append-system-prompt", system]
    return argv + (["--model", model] if model else [])


def run_agent(d: Path, argv: list[str], timeout: float, env: dict[str, str]) -> None:
    """Run the agent in run directory `d` on its prompt.txt; write agent.json, then done."""
    prompt = (d / "prompt.txt").read_text()
    home = Path(env.get("HOME") or tempfile.gettempdir())
    conf = Path(tempfile.mkdtemp(prefix="claude-", dir=home))  # nothing carries over from another run
    code: Any = None
    try:
        p = subprocess.Popen([*argv, prompt], cwd=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**env, "HOME": str(conf), "CLAUDE_CONFIG_DIR": str(conf)},
                             start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            out, err, code = "", f"timed out after {timeout:.0f}s", "timeout"
        finally:
            try:  # whatever it left running goes with it
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait()
        (d / "agent.json").write_text(out or "")
        (d / "done").write_text(json.dumps({"code": code, "stderr": (err or "")[-2000:]}))
    finally:
        shutil.rmtree(conf, ignore_errors=True)


def agent_loop(spool: Path, argv: list[str], timeout: float, env: dict[str, str], poll: float = 2) -> None:
    q = spool / "queue"
    while True:
        for f in sorted(q.iterdir(), key=lambda x: x.stat().st_mtime):
            rid = f.name
            try:
                if RUN_ID.match(rid) and (spool / "runs" / rid / "prompt.txt").is_file():
                    log(f"run {rid}: agent started")
                    run_agent(spool / "runs" / rid, argv, timeout, env)
            except Exception:  # noqa: BLE001 - the service waits out its timeout
                traceback.print_exc()
            finally:
                f.unlink(missing_ok=True)
        time.sleep(poll)


# ── the service's side ───────────────────────────────────────────────────
class Reviewer:
    def __init__(self, server: Server, spool: Path, timeout: float, poll: float = 2):
        self.server, self.spool, self.timeout, self.poll = server, spool, timeout, poll
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
        d = self.spool / "runs" / rid
        shutil.rmtree(d, ignore_errors=True)  # an earlier attempt's files are not this one's
        (d / "jobs").mkdir(parents=True)
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
            (d / "jobs" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.json").write_text(json.dumps(job, indent=1))
        (d / "prompt.txt").write_text(
            f"Review fleetkit run {rid} ({run.get('repo')} {str(run.get('base'))[:12]}..{run['sha'][:12]}) as "
            "the review-run instructions say. Its files are in this directory: run.json, changes.json "
            "(commits, tickets), diff.patch, jobs/*.json (the deploy jobs' events).")
        return d

    def ask(self, rid: str, d: Path) -> Optional[str]:
        """Queue the run for the agent and wait for its answer; None: none in time."""
        (self.spool / "queue" / rid).touch()
        end = time.time() + self.timeout + 120
        while time.time() < end:
            if (d / "done").is_file():
                done = json.loads((d / "done").read_text() or "{}")
                if done.get("code") != 0:
                    log(f"run {rid}: agent ended with {done.get('code')}: {str(done.get('stderr'))[-500:]}")
                return (d / "agent.json").read_text()
            time.sleep(self.poll)
        log(f"run {rid}: no answer from the agent in time")
        return None

    def review(self, rid: str) -> None:
        run = self.server.call("GET", f"/v1/runs/{rid}")
        if run.get("state") != "reviewing":
            log(f"run {rid} is {run.get('state')}; not reviewed")
            return
        t0 = time.time()
        out = self.ask(rid, self.layout(rid, run))
        v = verdict_of(out) if out else None
        if v is None:
            log(f"run {rid}: no valid verdict after {time.time() - t0:.0f}s; none posted")
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
    want = f"Bearer {token}".encode()

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
            got = self.headers.get("Authorization", "").encode("utf-8", "surrogateescape")
            if not hmac.compare_digest(got, want):
                return self.reply(401, {"error": "bad token"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= 65536:
                return self.reply(413, {"error": "bad length"})
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


def main(agent: bool = False) -> None:
    env = os.environ.get
    spool = Path(env("FLEETKIT_REVIEWER_SPOOL") or "spool").resolve()
    timeout = float(env("FLEETKIT_REVIEWER_TIMEOUT") or 1200)
    if agent:
        if not env("FLEETKIT_REVIEWER_PROMPT_FILE"):
            raise ValueError("fleetkit reviewer --agent needs FLEETKIT_REVIEWER_PROMPT_FILE")
        argv = agent_argv(env("FLEETKIT_REVIEWER_PROMPT_FILE", ""), env("FLEETKIT_REVIEWER_NOTES_FILE"),
                          (env("FLEETKIT_REVIEWER_COMMANDS") or "fleet-ssh fleet-get").split(),
                          env("FLEETKIT_REVIEWER_AGENT") or "claude", env("FLEETKIT_REVIEWER_MODEL"))
        log(f"agent: spool {spool}")
        agent_loop(spool, argv, timeout, {k: os.environ[k] for k in (*AGENT_ENV, "HOME") if k in os.environ})
        return
    server, rtoken, btoken = env("FLEETKIT_SERVER"), _read_secret("FLEETKIT_REVIEWER_TOKEN"), \
        _read_secret("FLEETKIT_RUNNER_TOKEN")
    if not (server and rtoken and btoken):
        raise ValueError("fleetkit reviewer needs FLEETKIT_SERVER, FLEETKIT_REVIEWER_TOKEN(_FILE) and "
                         "FLEETKIT_RUNNER_TOKEN(_FILE)")
    rv = Reviewer(Server(server, rtoken), spool, timeout)
    threading.Thread(target=rv.work, name="review", daemon=True).start()
    host, _, port = (env("FLEETKIT_REVIEWER_LISTEN") or "0.0.0.0:8741").rpartition(":")
    log(f"listening on {host}:{port}, deploy server {server}, spool {spool}")
    ThreadingHTTPServer((host, int(port)), handler(rv, btoken)).serve_forever()
