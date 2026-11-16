"""Verified runs (runs.py) with fake forge, tracker, runner and deployer, and a
local git repo as the remote (the revert is a real commit pushed to it): the
merge guard, one run per repository, the preview gate, and the verdicts pass,
fail and timeout. Offline."""
import subprocess

import pytest
from fastapi.testclient import TestClient

from fleetkit_cli.api import create_app
from fleetkit_cli.gitops import GitOps, Repo
from fleetkit_cli.jobs import JobManager
from fleetkit_cli.runs import Pipeline
from fleetkit_cli.settings import GitSettings, Settings

REPO = "acme/estate"
CHECKS = ("ci / flake checks", "ticket-key")


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, msg):
    (repo / name).write_text(text)
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", msg, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


class Forge:
    def __init__(self):
        self.prs, self.checked, self.moved, self.opened = {}, {}, [], []

    def merge(self, sha, n, base="unstable", checks=None):
        self.prs[sha] = {"number": n, "base": {"ref": base}, "head": {"sha": f"head{n}"},
                         "merge_commit_sha": sha}
        self.checked[f"head{n}"] = {c: "success" for c in CHECKS} if checks is None else checks

    def merged_pr(self, repo, sha):
        return self.prs.get(sha)

    def checks(self, repo, sha):
        return self.checked.get(sha, {})

    def fast_forward(self, repo, branch, sha):
        self.moved.append((branch, sha))

    def open_pr(self, repo, head, base, title, body):
        self.opened.append((head, base, title))
        n = 900 + len(self.opened)
        return {"number": n, "html_url": f"https://example.com/{repo}/pull/{n}"}


class Tracker:
    pattern = r"[A-Z]+-[0-9]+"

    def __init__(self):
        self.states, self.comments, self.links, self.labels = {}, [], [], {}

    def get(self, key):
        return {"key": key, "labels": self.labels.get(key, [])}

    def comment(self, key, text):
        self.comments.append((key, text))

    def set_state(self, key, state):
        self.states[key] = state

    def link(self, key, url, title):
        self.links.append((key, url))


class Runner:
    def __init__(self):
        self.events = []

    def dispatch(self, event):
        self.events.append(event)


class Deployer:
    def __init__(self):
        self.calls, self.plan = [], {}

    def deploy(self, estate, sha, preview):
        self.calls.append((estate, sha, preview))
        return {"state": "succeeded", "job": f"j{len(self.calls)}",
                "result": {"plan": self.plan if preview else {}, "refused": {}}}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "unstable", cwd=origin)
    base = commit(origin, "app.txt", "one\n", "start")
    git("branch", "stable", cwd=origin)
    s = Settings(flake=None, state_dir=tmp_path / "state", passphrase="p",
                 git=GitSettings(url=str(origin), branch="unstable", deploy_on_push=["nj", "github"]))
    forge, tracker, runner, deployer, clock = Forge(), Tracker(), Runner(), Deployer(), Clock()
    p = Pipeline(s.state_dir, "unstable", ["nj", "github"], forge, tracker, runner, deployer, Repo(s),
                 required=CHECKS, protect=("*/btc-mainnet*",), timeout=1800, spawn=lambda f: f(), clock=clock,
                 alert=lambda m: alerts.append(m))
    alerts = []

    class E:
        pass

    e = E()
    e.__dict__.update(origin=origin, base=base, p=p, forge=forge, tracker=tracker, runner=runner,
                      deployer=deployer, clock=clock, alerts=alerts, s=s)
    return e


def merge(e, n, text, ticket="INFRA-1"):
    sha = commit(e.origin, "app.txt", text, f"{ticket}: change {n} (#{n})")
    e.forge.merge(sha, n)
    return sha


def only(p, state):
    rs = p.list(state)
    assert len(rs) == 1, [(r["state"], r["log"]) for r in p.list()]
    return rs[0]


# ── the guard ────────────────────────────────────────────────────────────
def test_direct_push_is_refused(env):
    sha = commit(env.origin, "app.txt", "pushed\n", "no PR")
    out = env.p.trigger(REPO, sha, "test")
    assert "not the merge commit" in out["refused"]
    assert env.deployer.calls == [] and env.alerts


def test_red_or_missing_check_is_refused(env):
    sha = commit(env.origin, "app.txt", "x\n", "INFRA-1: x")
    env.forge.merge(sha, 7, checks={"ci / flake checks": "failure"})
    out = env.p.trigger(REPO, sha, "test")
    assert "ci / flake checks: failure" in out["refused"] and "ticket-key: missing" in out["refused"]
    assert env.deployer.calls == []


def test_merge_into_another_branch_is_refused(env):
    sha = commit(env.origin, "app.txt", "x\n", "x")
    env.forge.merge(sha, 8, base="feature")
    assert "not unstable" in env.p.trigger(REPO, sha, "test")["refused"]
    assert env.deployer.calls == []


# ── a run, and the queue ─────────────────────────────────────────────────
def test_green_merge_previews_deploys_and_asks_for_review(env):
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    r = only(env.p, "reviewing")
    assert env.deployer.calls == [("nj", sha, True), ("github", sha, True), ("nj", sha, False),
                                  ("github", sha, False)]
    assert r["base"] == env.base and r["tickets"] == ["INFRA-1"]
    assert env.runner.events[-1]["event"] == "review.requested"
    assert env.runner.events[-1]["range"] == {"base": env.base, "head": sha}


def test_one_run_per_repo_and_the_newest_queued_merge_runs_next(env):
    a = merge(env, 1, "a\n")
    env.p.trigger(REPO, a, "test")
    b = merge(env, 2, "b\n", "INFRA-2")
    c = merge(env, 3, "c\n", "INFRA-3")
    assert env.p.trigger(REPO, b, "test")["started"] is None
    assert env.p.trigger(REPO, c, "test")["started"] is None
    assert len(env.deployer.calls) == 4  # only the first run's
    first = only(env.p, "reviewing")
    env.p.verdict(first["id"], "pass", "looks right", "reviewer")
    nxt = only(env.p, "reviewing")
    assert nxt["sha"] == c  # b is folded into c's range
    assert {x[1] for x in env.deployer.calls[4:]} == {c}


def test_delete_or_replace_stops_before_anything_is_applied(env):
    env.deployer.plan = {"nj-guests": [{"op": "replace", "key": "app-1", "type": "proxmoxve:VM"}]}
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    r = only(env.p, "needs-operator")
    assert "replace nj-guests/app-1" in r["log"][-1]["msg"]
    assert all(preview for _, _, preview in env.deployer.calls)
    assert env.alerts


def test_change_to_a_protected_guest_stops(env):
    env.deployer.plan = {"nj-guests": [{"op": "update", "key": "btc-mainnet", "type": "proxmoxve:VM"}]}
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    assert "(protected)" in only(env.p, "needs-operator")["log"][-1]["msg"]
    assert all(preview for _, _, preview in env.deployer.calls)


# ── verdicts ─────────────────────────────────────────────────────────────
def test_pass_fast_forwards_stable_and_closes_tickets(env):
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    r = env.p.verdict(only(env.p, "reviewing")["id"], "pass", "service healthy", "reviewer")
    assert r["state"] == "passed"
    assert env.forge.moved == [("stable", sha)]
    assert env.tracker.states == {"INFRA-1": "Done"}
    assert "service healthy" in env.tracker.comments[0][1]
    with pytest.raises(ValueError):  # a second verdict
        env.p.verdict(r["id"], "fail", "x", "reviewer")


def test_fail_redeploys_stable_opens_a_revert_and_hands_back(env):
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    rid = only(env.p, "reviewing")["id"]
    env.deployer.calls.clear()
    r = env.p.verdict(rid, "fail", "port 443 refused", "reviewer")
    assert r["state"] == "reverting"
    assert env.deployer.calls == [("nj", env.base, False), ("github", env.base, False)]
    assert env.forge.moved == []  # stable stays
    branch = f"fleetkit/revert-{rid}"
    assert env.forge.opened[0][:2] == (branch, "unstable")
    # The revert is a real commit on unstable's head that restores stable's tree.
    assert git("show", f"{branch}:app.txt", cwd=env.origin) == "one"
    assert git("rev-parse", f"{branch}^", cwd=env.origin) == sha
    assert env.tracker.states == {"INFRA-1": "To Do"}
    assert "port 443 refused" in env.tracker.comments[0][1] and env.tracker.links
    ev = env.runner.events[-1]
    assert ev["event"] == "work.requested" and ev["findings"] == "port 443 refused"
    # The queue waits for the revert; its merge lets it move.
    later = merge(env, 2, "three\n", "INFRA-2")
    assert env.p.trigger(REPO, later, "test")["started"] is None
    rev = commit(env.origin, "app.txt", "one\n", "Merge revert")
    env.forge.merge(rev, r["revert_pr"])
    assert env.p.trigger(REPO, rev, "test")["started"]
    assert env.p.get(rid)["state"] == "failed"


def test_fail_of_a_no_auto_revert_ticket_stops_for_a_person(env):
    env.tracker.labels["INFRA-1"] = ["no-auto-revert"]
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    env.deployer.calls.clear()
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator" and env.deployer.calls == [] and env.forge.opened == []


def test_conflicting_revert_stops_for_a_person(env):
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    rid = only(env.p, "reviewing")["id"]
    commit(env.origin, "app.txt", "someone else\n", "a later push")  # the revert no longer applies
    r = env.p.verdict(rid, "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator" and "could not be opened" in r["log"][-1]["msg"]


def test_timeout_stops_the_queue_and_never_reverts(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    rid = only(env.p, "reviewing")["id"]
    calls = len(env.deployer.calls)
    env.clock.now += 1801
    env.p.tick()
    assert env.p.get(rid)["state"] == "timed-out"
    assert len(env.deployer.calls) == calls and env.forge.opened == [] and env.forge.moved == []
    assert env.alerts
    nxt = merge(env, 2, "three\n", "INFRA-2")
    assert env.p.trigger(REPO, nxt, "test")["started"] is None  # stopped
    env.p.resolve(rid, "checked by hand", "operator")
    assert only(env.p, "reviewing")["sha"] == nxt


def test_restart_mid_deploy_needs_a_person(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    r = only(env.p, "reviewing")
    r["state"] = "deploying"
    env.p._save()
    p2 = Pipeline(env.s.state_dir, "unstable", ["nj"], env.forge, None, None, env.deployer, None)
    assert p2.get(r["id"])["state"] == "needs-operator"


# ── over HTTP ────────────────────────────────────────────────────────────
def test_api(env):
    m = JobManager(env.s.state_dir, lambda req, ev: {})
    g = GitOps(env.s, Repo(env.s), m)
    g.runs = env.p
    c = TestClient(create_app(m, env.s, "t0ken", g))
    h = {"Authorization": "Bearer t0ken"}
    sha = merge(env, 1, "two\n")
    assert c.post("/v1/runs", json={"repo": REPO, "sha": sha}).status_code == 401
    assert c.post("/v1/runs", json={"repo": REPO, "sha": sha}, headers=h).json()["started"]
    rid = c.get("/v1/runs?state=reviewing", headers=h).json()[0]["id"]
    assert c.post(f"/v1/runs/{rid}/verdict", json={"verdict": "maybe", "evidence": ""}, headers=h).status_code == 422
    r = c.post(f"/v1/runs/{rid}/verdict", json={"verdict": "pass", "evidence": "ok"}, headers=h).json()
    assert r["state"] == "passed" and r["verdict"]["by"] == "api-token"
    assert c.post(f"/v1/runs/{rid}/verdict", json={"verdict": "pass", "evidence": "ok"},
                  headers=h).status_code == 409
    assert c.get("/v1/runs/nope", headers=h).status_code == 404
