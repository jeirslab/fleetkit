"""The reviewer (reviewer.py) with a fake deploy server and a fake agent: a
valid answer is posted, anything else posts nothing (the server's timeout
covers it), a run no longer reviewing is left alone, and the runner endpoint
takes only the deploy server's bearer. Offline."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from fleetkit_cli import reviewer


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


def agent(answer):
    # A stand-in for `claude -p --output-format json`: prints `answer`, ignores the prompt.
    return [sys.executable, "-c", f"print({json.dumps(json.dumps(answer))})"]


def test_a_valid_answer_is_posted_and_the_run_laid_out(tmp_path):
    s = FakeServer()
    rv = reviewer.Reviewer(s, tmp_path, agent({"structured_output": {"verdict": "pass", "evidence": "ok"}}), 30)
    rv.review("r1")
    assert s.posted == [("/v1/runs/r1/verdict", {"verdict": "pass", "evidence": "ok"})]
    d = tmp_path / "r1"
    assert (d / "diff.patch").read_text() == "+x\n" and "diff" not in json.loads((d / "changes.json").read_text())
    assert json.loads((d / "jobs" / "deploy_nj.json").read_text())["events"]
    assert [p.name for p in (d / "jobs").iterdir()] == ["deploy_nj.json"]  # no path from a job id


def test_no_valid_answer_posts_nothing(tmp_path):
    for answer in ({"result": "I could not decide"}, {"structured_output": {"verdict": "pass", "evidence": " "}},
                   {"structured_output": {"verdict": "maybe", "evidence": "x"}}):
        s = FakeServer()
        reviewer.Reviewer(s, tmp_path, agent(answer), 30).review("r1")
        assert s.posted == []
    s = FakeServer()
    reviewer.Reviewer(s, tmp_path, [sys.executable, "-c", "import sys; sys.exit(1)"], 30).review("r1")
    assert s.posted == []


def test_fail_from_the_last_message_and_a_run_not_reviewing(tmp_path):
    s = FakeServer()
    a = agent({"result": 'checked\n{"verdict": "fail", "evidence": "unit x failed"}'})
    reviewer.Reviewer(s, tmp_path, a, 30).review("r1")
    assert s.posted == [("/v1/runs/r1/verdict", {"verdict": "fail", "evidence": "unit x failed"})]
    s = FakeServer(state="passed")
    reviewer.Reviewer(s, tmp_path / "late", [sys.executable, "-c", "raise SystemExit(1)"], 30).review("r1")
    assert s.posted == [] and not (tmp_path / "late").exists()


def test_the_runner_endpoint(tmp_path):
    rv = reviewer.Reviewer(FakeServer(), tmp_path, [], 30)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), reviewer.handler(rv, "s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"

    def post(body, token="s3cret"):
        r = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                   headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    try:
        assert post({"event": "review.requested", "run": "r1"}, token="nope")[0] == 401
        assert post({"event": "work.requested", "run": "r1"}) == (202, {"ignored": "work.requested"})
        assert post({"event": "review.requested", "run": "../r1"})[0] == 400
        assert post({"event": "review.requested", "run": "r1"}) == (202, {"queued": True, "run": "r1"})
        assert post({"event": "review.requested", "run": "r1"}) == (202, {"queued": False, "run": "r1"})
        assert rv.q.get_nowait() == "r1"
    finally:
        httpd.shutdown()
