"""The reviewer (reviewer.py) with a fake deploy server and a fake agent,
over the real spool between the service and the agent: only claude's
structured output with no error is a verdict, anything else posts nothing
(the server's timeout covers it), a run no longer reviewing is left alone,
the agent gets no token in its environment, and the runner endpoint takes
only the deploy server's bearer. Offline."""
import http.client
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from fleetkit_cli import reviewer

OK = {"type": "result", "subtype": "success", "is_error": False}


class FakeServer:
    def __init__(self, state="reviewing"):
        self.state, self.posted = state, []
        self.run = {"id": "r1", "repo": "acme/estate", "base": "a" * 40, "sha": "b" * 40, "state": state,
                    "jobs": {"deploy:nj": "j1", "deploy-preview:nj": "../x"}}

    def call(self, method, path, body=None):
        if method == "POST":
            self.posted.append((path, body))
            return {}
        if path == "/v1/runs/r1":
            return {**self.run, "state": self.state}
        if path == "/v1/runs/r1/diff":
            return {"commits": [{"sha": "b" * 40, "subject": "T-1: x"}], "diff": "+x\n", "tickets": []}
        if path == "/v1/deploys/j1":
            return {"id": "j1", "state": "succeeded"}
        if path.startswith("/v1/deploys/j1/events"):
            return {"events": [{"seq": 0, "msg": "applied"}]}
        raise AssertionError(path)


def spool(tmp_path):
    for d in ("queue", "runs"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


def agent(tmp_path, answer, env=None):
    """A stand-in for claude in the agent's loop, in a thread: it prints `answer`
    (and writes its environment beside it)."""
    code = ("import json, os, sys; json.dump(dict(os.environ), open('env.json', 'w')); "
            f"print({json.dumps(json.dumps(answer))})")
    threading.Thread(target=reviewer.agent_loop, daemon=True,
                     args=(tmp_path, [sys.executable, "-c", code], 30, {"HOME": str(tmp_path), **(env or {})}, 0.02)
                     ).start()


def review(tmp_path, s, answer, env=None):
    agent(spool(tmp_path), answer, env)
    reviewer.Reviewer(s, tmp_path, 10, poll=0.02).review("r1")


def test_a_valid_answer_is_posted_and_the_agent_holds_no_token(tmp_path):
    s = FakeServer()
    review(tmp_path, s, {**OK, "structured_output": {"verdict": "pass", "evidence": "ok"}},
           env={"ANTHROPIC_API_KEY": "k", "PATH": "/bin"})
    assert s.posted == [("/v1/runs/r1/verdict", {"verdict": "pass", "evidence": "ok"})]
    d = tmp_path / "runs" / "r1"
    assert (d / "diff.patch").read_text() == "+x\n" and "diff" not in json.loads((d / "changes.json").read_text())
    assert [p.name for p in (d / "jobs").iterdir()] == ["deploy_nj.json"]  # no path from a job id
    env = json.loads((d / "env.json").read_text())
    assert env["ANTHROPIC_API_KEY"] == "k" and not any(k.startswith("FLEETKIT") for k in env)
    assert env["CLAUDE_CONFIG_DIR"] == env["HOME"] != str(tmp_path)  # a fresh config dir, removed after
    assert not list((tmp_path / "queue").iterdir())


def test_only_structured_output_without_error_is_a_verdict(tmp_path):
    for answer in ({**OK, "result": '{"verdict": "pass", "evidence": "x"}'},
                   {**OK, "structured_output": {"verdict": "pass", "evidence": " "}},
                   {**OK, "structured_output": {"verdict": "maybe", "evidence": "x"}},
                   {**OK, "is_error": True, "structured_output": {"verdict": "pass", "evidence": "x"}},
                   {**OK, "subtype": "error_max_turns", "structured_output": {"verdict": "pass", "evidence": "x"}}):
        assert reviewer.verdict_of(json.dumps(answer)) is None, answer
    s = FakeServer()
    review(tmp_path, s, {**OK, "result": '{"verdict": "pass", "evidence": "x"}'})
    assert s.posted == []
    s = FakeServer()
    review(tmp_path / "f", s, {**OK, "structured_output": {"verdict": "fail", "evidence": "unit x failed"}})
    assert s.posted == [("/v1/runs/r1/verdict", {"verdict": "fail", "evidence": "unit x failed"})]


def test_a_run_not_reviewing_is_left_alone_and_a_silent_agent_posts_nothing(tmp_path):
    s = FakeServer(state="passed")
    reviewer.Reviewer(s, spool(tmp_path), 0.1, poll=0.02).review("r1")
    assert s.posted == [] and not (tmp_path / "runs" / "r1").exists()
    s = FakeServer()
    rv = reviewer.Reviewer(s, spool(tmp_path), 0.1, poll=0.02)
    rv.timeout = -119.8  # no agent runs: the wait ends at once
    rv.review("r1")
    assert s.posted == [] and (tmp_path / "queue" / "r1").exists()
    assert not (tmp_path / "runs" / "r1" / "attempt").exists()  # given up: an agent skips it


def test_the_runner_endpoint(tmp_path):
    rv = reviewer.Reviewer(FakeServer(), spool(tmp_path), 30)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), reviewer.handler(rv, "s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def post(body, token="s3cret"):
        r = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(), method="POST",
                                   headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def raw(headers):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest("POST", "/")
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        return c.getresponse().status

    try:
        assert post({"event": "review.requested", "run": "r1"}, token="nope")[0] == 401
        assert raw({"Authorization": "Bearer s3cret", "Content-Length": "-1"}) == 413
        assert raw({"Authorization": "Bearer s3cret", "Content-Length": "x"}) == 413
        assert raw({"Authorization": "Bearer \xe9"}) == 401
        assert post({"event": "work.requested", "run": "r1"}) == (202, {"ignored": "work.requested"})
        assert post({"event": "review.requested", "run": "../r1"})[0] == 400
        assert post({"event": "review.requested", "run": "r1"}) == (202, {"queued": True, "run": "r1"})
        assert post({"event": "review.requested", "run": "r1"}) == (202, {"queued": False, "run": "r1"})
        assert rv.q.get_nowait() == "r1"
    finally:
        httpd.shutdown()


def test_the_agents_command_line(tmp_path):
    (tmp_path / "skill.md").write_text("S")
    argv = reviewer.agent_argv(str(tmp_path / "skill.md"), None, ["fleet-ssh", "fleet-get"])
    assert argv[argv.index("--allowedTools") + 1] == "Bash(fleet-ssh:*) Bash(fleet-get:*)"
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Grep,Glob" and "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "" and argv[-1] == "S"


def test_an_earlier_attempts_answer_is_not_this_ones(tmp_path):
    sp = spool(tmp_path)
    d = sp / "runs" / "r1"
    d.mkdir(parents=True)
    # An agent of an earlier attempt finishing late, into the new attempt's directory.
    (d / "agent.old.json").write_text(json.dumps({**OK, "structured_output": {"verdict": "fail", "evidence": "x"}}))
    (d / "done").write_text(json.dumps({"code": 0, "nonce": "old"}))
    rv = reviewer.Reviewer(FakeServer(), sp, -119.8, poll=0.02)
    assert rv.ask("r1", d) is None
    # A queued entry whose nonce is not the run's current attempt is taken and dropped, never run.
    (sp / "queue" / "r1").write_text("stale")
    agent(sp, {**OK, "structured_output": {"verdict": "pass", "evidence": "x"}})
    for _ in range(200):
        if not list((sp / "queue").iterdir()):
            break
        threading.Event().wait(0.02)
    assert not list((sp / "queue").iterdir()) and not (d / "env.json").exists()
