"""The API and job manager with a fake runner: auth, the job lifecycle, one
deploy per estate, cancel, the event log, and restart recovery. Offline."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fleetkit_cli.api import create_app
from fleetkit_cli.events import Emitter
from fleetkit_cli.jobs import JobManager
from fleetkit_cli.pipeline import DeployRequest

TOKEN = "t0ken"
H = {"Authorization": f"Bearer {TOKEN}"}


class Fake:
    """A runner whose jobs wait on a gate, so tests can see them running."""

    def __init__(self):
        self.gate = threading.Event()
        self.fail = False

    def __call__(self, req: DeployRequest, ev: Emitter):
        ev.emit("infra", "step", op="create", urn=f"urn:{req.estate}")
        while not self.gate.wait(0.01):
            ev.check()
        if self.fail:
            raise RuntimeError("boom")
        return {"infra": {"guests": {"create": 1}}, "nixos": req.goal}


@pytest.fixture
def env(tmp_path):
    fake = Fake()
    m = JobManager(tmp_path, fake)
    return TestClient(create_app(m, None, TOKEN)), fake, m, tmp_path


def wait_state(c, jid, *states):
    for _ in range(500):
        r = c.get(f"/v1/deploys/{jid}", headers=H).json()
        if r["state"] in states:
            return r
        time.sleep(0.01)
    raise AssertionError(f"job {jid} never reached {states}: {r}")


def test_auth(env):
    c, *_ = env
    assert c.get("/healthz").status_code == 200
    assert c.get("/v1/deploys").status_code == 401
    assert c.get("/v1/deploys", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/deploys", headers=H).status_code == 200


def test_lifecycle_and_events(env):
    c, fake, *_ = env
    r = c.post("/v1/deploys", json={"estate": "mini", "goal": "test"}, headers=H)
    assert r.status_code == 202
    jid = r.json()["id"]
    wait_state(c, jid, "running")
    fake.gate.set()
    rec = wait_state(c, jid, "succeeded")
    assert rec["result"] == {"infra": {"guests": {"create": 1}}, "nixos": "test"}
    ev = c.get(f"/v1/deploys/{jid}/events", headers=H).json()
    assert [e["kind"] for e in ev["events"]] == ["step", "succeeded"]
    assert [e["seq"] for e in ev["events"]] == [0, 1]
    assert c.get(f"/v1/deploys/{jid}/events?after=1", headers=H).json()["events"][0]["kind"] == "succeeded"


def test_stream_ends_with_record(env):
    c, fake, *_ = env
    jid = c.post("/v1/deploys", json={"estate": "mini"}, headers=H).json()["id"]
    fake.gate.set()
    wait_state(c, jid, "succeeded")
    body = c.get(f"/v1/deploys/{jid}/stream", headers=H).text
    assert "event: end" in body and '"kind": "succeeded"' in body


def test_one_deploy_per_estate(env):
    c, fake, *_ = env
    a = c.post("/v1/deploys", json={"estate": "mini"}, headers=H).json()["id"]
    r = c.post("/v1/deploys", json={"estate": "mini"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["job"] == a
    # Another estate is not blocked.
    assert c.post("/v1/deploys", json={"estate": "other"}, headers=H).status_code == 202
    fake.gate.set()
    wait_state(c, a, "succeeded")
    assert c.post("/v1/deploys", json={"estate": "mini"}, headers=H).status_code == 202


def test_failure_is_reported(env):
    c, fake, *_ = env
    fake.fail = True
    fake.gate.set()
    jid = c.post("/v1/deploys", json={"estate": "mini"}, headers=H).json()["id"]
    rec = wait_state(c, jid, "failed")
    assert rec["error"] == "RuntimeError: boom"


def test_cancel(env):
    c, *_ = env
    jid = c.post("/v1/deploys", json={"estate": "mini"}, headers=H).json()["id"]
    wait_state(c, jid, "running")
    c.post(f"/v1/deploys/{jid}/cancel", headers=H)
    wait_state(c, jid, "cancelled")


def test_bad_request(env):
    c, *_ = env
    assert c.post("/v1/deploys", json={"estate": "mini", "goal": "yolo"}, headers=H).status_code == 422
    assert c.get("/v1/deploys/nope", headers=H).status_code == 404


def test_restart_marks_running_interrupted(env):
    c, fake, m, tmp = env
    jid = c.post("/v1/deploys", json={"estate": "mini"}, headers=H).json()["id"]
    wait_state(c, jid, "running")
    # A second manager over the same directory is a restarted server.
    m2 = JobManager(tmp, Fake())
    j = m2.jobs[jid]
    assert j.state == "interrupted" and j.events[0]["kind"] == "step"
    assert json.loads((tmp / "jobs" / f"{jid}.json").read_text())["state"] == "interrupted"
    fake.gate.set()
