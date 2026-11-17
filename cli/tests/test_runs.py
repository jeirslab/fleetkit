"""Verified runs (runs.py) with fake forge, tracker, runner and deployer, and a
local git repo as the remote (the revert is a real commit pushed to it): the
merge guard over the whole range, one run per repository, the preview gate on
the way forward and back, the verdicts pass, fail and timeout, and the HTTP
routes with a reviewer's scoped token. GitHub.checks against real-shaped
payloads. Offline."""
import hashlib
import hmac
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from fleetkit_cli import adapters, nixos
from fleetkit_cli.events import Emitter
from fleetkit_cli.api import create_app
from fleetkit_cli.github import GitHub
from fleetkit_cli.gitops import GitOps, Repo
from fleetkit_cli.jobs import JobManager
from fleetkit_cli.runs import Pipeline
from fleetkit_cli.settings import GitSettings, Settings
from fleetkit_cli.tokens import Scope, Token, _digest

REPO = "acme/estate"
CHECKS = ("flake checks", "ticket-key")  # check-run names: a job's own name


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, text, msg):
    (repo / "app.txt").write_text(text)
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", msg, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


class Forge:
    def __init__(self):
        self.prs, self.checked, self.moved, self.opened = {}, {}, [], []

    def merge(self, sha, n, base="unstable", checks=None):
        self.prs[sha] = {"number": n, "base": {"ref": base}, "head": {"sha": f"head{n}"}, "merge_commit_sha": sha}
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

    down = False

    def get(self, key):
        if self.down:
            raise RuntimeError("503")
        if key == "NOPE-1":
            return None
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
        self.calls, self.plans, self.nixos = [], {}, {}

    def deploy(self, estate, sha, preview, on):
        self.calls.append((estate, sha, preview, on))
        result = {"plan": self.plans.get(sha, {}) if preview else {}, "refused": {}}
        if preview and on:  # the compare a JobDeployer asks for (nixos.changes); unchanged by default
            same = {n: {"built": "/nix/store/a-system", "live": "/nix/store/a-system", "tags": []} for n in on}
            result["nixos_changes"] = {**same, **self.nixos.get(sha, {})}
        return {"state": "succeeded", "job": f"j{len(self.calls)}", "result": result}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.origin = tmp_path / "origin"
    e.origin.mkdir()
    git("init", "-q", "-b", "unstable", cwd=e.origin)
    e.base = commit(e.origin, "one\n", "start")
    git("branch", "stable", cwd=e.origin)
    e.s = Settings(flake=None, state_dir=tmp_path / "state", passphrase="p",
                   git=GitSettings(url=str(e.origin), branch="unstable", deploy_on_push=["nj", "github"]))
    e.forge, e.tracker, e.runner, e.deployer, e.clock, e.alerts = Forge(), Tracker(), Runner(), Deployer(), Clock(), []
    e.repo = Repo(e.s)
    e.p = Pipeline(e.s.state_dir, REPO, "unstable", ["nj", "github"], e.forge, e.tracker, e.runner, e.deployer,
                   e.repo, required=CHECKS, protect=("*/btc-mainnet*",), nixos_on=("web-1",), timeout=1800,
                   spawn=lambda f: f(), clock=e.clock, alert=e.alerts.append)
    return e


def merge(e, n, text, ticket="INFRA-1"):
    sha = commit(e.origin, text, f"{ticket}: change {n} (#{n})\n\nSee ADR-167 and SHA-256.")
    e.forge.merge(sha, n)
    return sha


def only(p, state):
    rs = p.list(state)
    assert len(rs) == 1, [(r["state"], r["log"]) for r in p.list()]
    return rs[0]


def applied(e):
    return [c for c in e.deployer.calls if not c[2]]


# ── the guard ────────────────────────────────────────────────────────────
def test_direct_push_is_refused(env):
    out = env.p.trigger(REPO, commit(env.origin, "pushed\n", "no PR"), "test")
    assert "not the merge commit" in out["refused"]
    assert env.deployer.calls == [] and env.alerts


def test_red_or_missing_check_is_refused(env):
    sha = commit(env.origin, "x\n", "INFRA-1: x")
    env.forge.merge(sha, 7, checks={"flake checks": "failure"})
    out = env.p.trigger(REPO, sha, "test")
    assert "flake checks: failure" in out["refused"] and "ticket-key: missing" in out["refused"]
    assert env.deployer.calls == []


def test_merge_into_another_branch_is_refused(env):
    sha = commit(env.origin, "x\n", "x")
    env.forge.merge(sha, 8, base="feature")
    assert "not unstable" in env.p.trigger(REPO, sha, "test")["refused"]


def test_another_repository_is_rejected(env):
    with pytest.raises(ValueError):
        env.p.trigger("other/repo", merge(env, 1, "two\n"), "test")


def test_a_direct_push_folded_into_a_green_merge_stops_for_a_person(env):
    commit(env.origin, "pushed\n", "a direct push")
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    r = only(env.p, "needs-operator")
    assert "not the merge commit" in r["log"][-1]["msg"] and env.deployer.calls == []
    # A person vouches for the range: the same commit runs again.
    env.p.resolve(r["id"], "pushed by me, checked", "operator", accept=True)
    assert only(env.p, "reviewing")["sha"] == sha


def test_a_replayed_older_merge_is_refused(env):
    a = merge(env, 1, "a\n")
    env.p.trigger(REPO, a, "test")
    b = merge(env, 2, "b\n", "INFRA-2")
    env.p.trigger(REPO, b, "test")  # queued behind a
    calls = len(env.deployer.calls)
    # a again while it is under review: the same run, not a new one.
    assert "already queued or running" in env.p.trigger(REPO, a, "replay")["ignored"]
    env.p.verdict(only(env.p, "reviewing")["id"], "pass", "ok", "reviewer")
    git("update-ref", "refs/heads/stable", a, cwd=env.origin)  # what the fast-forward did
    env.p.verdict(only(env.p, "reviewing")["id"], "pass", "ok", "reviewer")  # b's run
    git("update-ref", "refs/heads/stable", b, cwd=env.origin)
    assert "not ahead of stable" in env.p.trigger(REPO, a, "replay")["refused"]
    assert {x[1] for x in env.deployer.calls[calls:]} == {b}
    # Older than the last deployed commit, though ahead of stable: refused too.
    c = merge(env, 3, "c\n", "INFRA-3")
    env.p.trigger(REPO, c, "test")
    env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    env.p.resolve(only(env.p, "reverting")["id"], "revert merged by hand elsewhere", "operator")
    assert "not newer" in env.p.trigger(REPO, c, "replay")["refused"]


# ── a run, and the queue ─────────────────────────────────────────────────
def test_green_merge_previews_deploys_and_asks_for_review(env):
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    r = only(env.p, "reviewing")
    on = ("web-1",)
    assert env.deployer.calls == [("nj", sha, True, on), ("github", sha, True, on), ("nj", sha, False, on),
                                  ("github", sha, False, on)]
    assert r["base"] == env.base and r["tickets"] == ["INFRA-1"]  # not ADR-167, not SHA-256
    assert env.runner.events[-1]["event"] == "review.requested"
    assert env.runner.events[-1]["range"] == {"base": env.base, "head": sha}


def test_one_run_per_repo_and_the_newest_queued_merge_runs_next(env):
    a = merge(env, 1, "a\n")
    env.p.trigger(REPO, a, "test")
    b = merge(env, 2, "b\n", "INFRA-2")
    c = merge(env, 3, "c\n", "INFRA-3")
    assert env.p.trigger(REPO, c, "test")["started"] is None
    assert "ignored" in env.p.trigger(REPO, b, "test")  # older: does not replace c
    assert len(env.deployer.calls) == 4  # only the first run's
    env.p.verdict(only(env.p, "reviewing")["id"], "pass", "looks right", "reviewer")
    nxt = only(env.p, "reviewing")
    assert nxt["sha"] == c  # b is folded into c's range
    assert {x[1] for x in env.deployer.calls[4:]} == {c}


def test_delete_or_replace_stops_before_anything_is_applied(env):
    sha = merge(env, 1, "two\n")
    env.deployer.plans[sha] = {"nj-guests": [{"op": "replace", "key": "app-1", "type": "proxmoxve:VM"}]}
    env.p.trigger(REPO, sha, "test")
    assert "replace nj-guests/app-1" in only(env.p, "needs-operator")["log"][-1]["msg"]
    assert applied(env) == [] and env.alerts


def test_change_to_a_protected_guest_stops(env):
    sha = merge(env, 1, "two\n")
    env.deployer.plans[sha] = {"nj-guests": [{"op": "update", "key": "btc-mainnet", "type": "proxmoxve:VM"}]}
    env.p.trigger(REPO, sha, "test")
    assert "(protected)" in only(env.p, "needs-operator")["log"][-1]["msg"]
    assert applied(env) == []


def test_a_protected_node_whose_toplevel_changes_stops(env):
    env.p.nixos_on = ("web-1", "btc-mainnet-1", "node-7")
    sha = merge(env, 1, "two\n")
    env.deployer.nixos[sha] = {
        "web-1": {"built": "/nix/store/b-system", "live": "/nix/store/a-system", "tags": []},  # not protected
        "btc-mainnet-1": {"built": "/nix/store/b-system", "live": "/nix/store/a-system", "tags": []},
        "node-7": {"built": "/nix/store/b-system", "live": "/nix/store/a-system", "tags": ["mainnet"]}}
    env.p.trigger(REPO, sha, "test")
    r = only(env.p, "needs-operator")
    msg = r["log"][-1]["msg"]
    assert "NixOS btc-mainnet-1: /nix/store/a-system -> /nix/store/b-system (protected)" in msg
    assert "NixOS node-7:" in msg and "web-1" not in msg and "nothing deployed" in msg
    assert any("toplevels change on btc-mainnet-1, node-7, web-1" in x["msg"] for x in r["log"])
    assert applied(env) == [] and env.alerts


def test_a_protected_node_that_does_not_change_is_never_touched(env):
    env.p.nixos_on = ("web-1", "btc-mainnet-1")
    sha = merge(env, 1, "two\n")
    env.deployer.nixos[sha] = {"web-1": {"built": "/nix/store/b-system", "live": "/nix/store/a-system", "tags": []}}
    env.p.trigger(REPO, sha, "test")
    r = only(env.p, "reviewing")
    # Left out of the apply: a hand deploy between preview and apply is not undone.
    assert {c[3] for c in applied(env)} == {("web-1",)} and r["skip_nodes"] == ["btc-mainnet-1"]
    assert any("not applied to btc-mainnet-1" in x["msg"] for x in r["log"])
    # The rollback previews afresh: the skip list is its own.
    env.deployer.calls.clear()
    env.p.verdict(r["id"], "fail", "bad", "reviewer")
    assert {c[3] for c in applied(env)} == {("web-1",)}


def test_the_job_deployer_asks_a_preview_to_compare():
    class Job:
        state, id, result, error, events = "succeeded", "j1", {}, None, []

    class Manager:
        jobs, active, reqs = {}, {}, []

        def submit(self, req, by):
            self.reqs.append(req)
            return Job()

    m = Manager()
    d = adapters.JobDeployer(m)
    d.deploy("nj", "abc", True, ("web-1",))
    d.deploy("nj", "abc", False, ("web-1",))
    d.deploy("nj", "abc", True, ())
    assert [(q.compare, q.nixos, q.on) for q in m.reqs] == [(True, True, ["web-1"]), (False, True, ["web-1"]),
                                                          (False, False, [])]


def test_a_scoped_token_may_not_compare():
    from fleetkit_cli import tokens
    from fleetkit_cli.pipeline import DeployRequest
    p = tokens.Principal("tenant", tokens.Scope(estates=("t",), hives=("t",), nixos="apply", allow=True))
    with pytest.raises(tokens.Refused) as e:
        tokens.check(p, DeployRequest(estate="t", infra=False, preview=True, on=["n"], compare=True))
    assert e.value.field == "compare"


def test_toplevels_that_cannot_be_compared_fail_closed(env):
    env.p.nixos_on = ("web-1", "btc-mainnet-1")
    sha = merge(env, 1, "two\n")
    env.deployer.nixos[sha] = {"web-1": {"built": "/nix/store/b-system", "live": None, "tags": []},
                               "btc-mainnet-1": {"built": "/nix/store/a-system", "live": None, "tags": []}}
    env.p.trigger(REPO, sha, "test")
    msg = only(env.p, "needs-operator")["log"][-1]["msg"]
    assert "btc-mainnet-1: the live system could not be read (protected)" in msg and "web-1" not in msg
    # A preview that did not compare at all (no nixos_changes) stops too.
    assert env.p.needs_operator({"plan": {}, "refused": {}}) == [
        "the NixOS toplevels were not compared with the live ones"]
    assert applied(env) == []


def test_the_way_back_stops_on_a_protected_node_too(env):
    env.p.nixos_on = ("web-1", "btc-mainnet-1")
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    # Someone moved the mainnet node meanwhile: the rollback would change it.
    env.deployer.nixos[env.base] = {
        "btc-mainnet-1": {"built": "/nix/store/a-system", "live": "/nix/store/c-system", "tags": []}}
    env.deployer.calls.clear()
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator" and "rollback of nj needs a person: NixOS btc-mainnet-1" in r["log"][-1]["msg"]
    assert applied(env) == [] and env.forge.opened == []


def test_a_queue_that_cannot_read_stable_is_left_as_it_was(env, monkeypatch):
    sha = merge(env, 1, "two\n")
    env.p.state["queued"] = {"sha": sha, "pr": 1, "at": 0, "source": "test"}
    monkeypatch.setattr(env.repo, "head", lambda b: (_ for _ in ()).throw(RuntimeError("fetch failed")))
    env.p.resume()
    assert env.p.state["queued"]["sha"] == sha and env.p.list() == [] and env.alerts


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
    on = ("web-1",)
    assert env.deployer.calls == [("nj", env.base, True, on), ("github", env.base, True, on),
                                  ("nj", env.base, False, on), ("github", env.base, False, on)]
    assert env.forge.moved == []  # stable stays
    branch, base, title = env.forge.opened[0]
    assert (branch, base) == (f"fleetkit/revert-{rid}", "unstable") and title.startswith("INFRA-1: Revert")
    # The revert is a real commit on unstable's head that restores stable's tree.
    assert git("show", f"{branch}:app.txt", cwd=env.origin) == "one"
    assert git("rev-parse", f"{branch}^", cwd=env.origin) == sha
    assert env.tracker.states == {"INFRA-1": "To Do"}
    assert "port 443 refused" in env.tracker.comments[0][1] and env.tracker.links
    ev = env.runner.events[-1]
    assert ev["event"] == "work.requested" and ev["findings"] == "port 443 refused"
    # The queue waits for the revert, and says so when it waits too long.
    later = merge(env, 2, "three\n", "INFRA-2")
    assert env.p.trigger(REPO, later, "test")["started"] is None
    env.alerts.clear()
    env.clock.now += 1801
    env.p.tick()
    assert env.alerts and env.p.get(rid)["state"] == "reverting"
    rev = commit(env.origin, "one\n", "INFRA-1: merge the revert")
    env.forge.merge(rev, r["revert_pr"])
    assert env.p.trigger(REPO, rev, "test")["started"]
    assert env.p.get(rid)["state"] == "failed"
    # The next run reviews INFRA-2, not the ticket whose work was reverted.
    assert only(env.p, "reviewing")["tickets"] == ["INFRA-2"]


def test_a_merged_revert_alone_returns_stable_to_its_own_tree_without_closing_the_ticket(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    rev = commit(env.origin, "one\n", "INFRA-1: Revert (merge)")
    env.forge.merge(rev, r["revert_pr"])
    env.p.trigger(REPO, rev, "test")
    assert only(env.p, "passed")["sha"] == rev and env.forge.moved == [("stable", rev)]
    assert env.tracker.states == {"INFRA-1": "To Do"}


def test_a_tracker_that_cannot_answer_holds_the_revert(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    env.tracker.down = True
    env.deployer.calls.clear()
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator" and "unreadable" in r["log"][-1]["msg"]
    assert env.deployer.calls == [] and env.forge.opened == []


def test_stable_moves_only_to_what_the_estates_run(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    with pytest.raises(ValueError, match="failed review"):
        env.p.resolve(r["id"], "x", "operator", stable=True)  # the reviewer failed it; it was rolled back
    env.p.resolve(r["id"], "revert merged by hand", "operator")
    stopped = merge(env, 3, "y\n", "INFRA-3")
    env.deployer.plans[stopped] = {"nj-guests": [{"op": "delete", "key": "a", "type": "proxmoxve:VM"}]}
    env.p.trigger(REPO, stopped, "test")
    run = only(env.p, "needs-operator")  # stopped before anything was applied
    with pytest.raises(ValueError, match="not what the estates run"):
        env.p.resolve(run["id"], "x", "operator", stable=True)


def test_the_same_merge_twice_is_one_run(env):
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "webhook")
    assert "already queued or running" in env.p.trigger(REPO, sha, "workflow")["ignored"]
    env.p.verdict(only(env.p, "reviewing")["id"], "pass", "ok", "reviewer")
    assert env.p.list("needs-operator") == [] and env.p.list("reviewing") == []


def test_accept_retries_a_run_that_had_deployed(env, monkeypatch):
    sha = merge(env, 1, "two\n")
    flaky = {"n": 0}
    real = env.deployer.deploy

    def deploy(estate, s, preview, on):
        out = real(estate, s, preview, on)
        if not preview and estate == "github" and not flaky["n"]:
            flaky["n"] = 1
            return {**out, "state": "failed", "error": "network"}
        return out

    monkeypatch.setattr(env.deployer, "deploy", deploy)
    env.p.trigger(REPO, sha, "test")
    r = only(env.p, "needs-operator")
    env.p.resolve(r["id"], "network back", "operator", accept=True)
    assert only(env.p, "reviewing")["sha"] == sha


def test_tick_starts_a_queued_merge_whose_start_failed(env, monkeypatch):
    sha = merge(env, 1, "two\n")
    env.p.state["queued"] = {"sha": sha, "pr": 1, "at": 0, "source": "test"}
    head = env.repo.head
    monkeypatch.setattr(env.repo, "head", lambda b: (_ for _ in ()).throw(RuntimeError("fetch failed")))
    env.p.tick()
    assert env.p.list() == []
    monkeypatch.setattr(env.repo, "head", head)
    env.p.tick()
    assert only(env.p, "reviewing")["sha"] == sha


def test_the_way_back_passes_the_same_gate(env):
    sha = merge(env, 1, "two\n")
    env.p.trigger(REPO, sha, "test")
    env.deployer.plans[env.base] = {"github": [{"op": "delete", "key": "repo-new", "type": "github:Repository"}]}
    env.deployer.calls.clear()
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator" and "delete github/repo-new" in r["log"][-1]["msg"]
    assert applied(env) == [] and env.forge.opened == []


def test_fail_of_a_no_auto_revert_ticket_stops_for_a_person(env):
    env.tracker.labels["INFRA-1"] = ["no-auto-revert"]
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    env.deployer.calls.clear()
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator" and env.deployer.calls == [] and env.forge.opened == []


def test_a_ticket_the_tracker_does_not_find_holds_the_revert(env):
    # A 404 is also what a ticket the account may not see answers.
    env.p.trigger(REPO, merge(env, 1, "two\n", "NOPE-1"), "test")
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator" and "not found" in r["log"][-1]["msg"]


def test_conflicting_revert_stops_for_a_person(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    rid = only(env.p, "reviewing")["id"]
    commit(env.origin, "someone else\n", "a later push")  # the revert no longer applies
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
    with pytest.raises(ValueError):  # too late for a verdict; /review asks again
        env.p.verdict(rid, "pass", "ok", "reviewer")
    nxt = merge(env, 2, "three\n", "INFRA-2")
    assert env.p.trigger(REPO, nxt, "test")["started"] is None  # stopped
    env.p.resolve(rid, "checked by hand", "operator", stable=True)
    assert env.forge.moved == [("stable", env.p.get(rid)["sha"])]
    assert only(env.p, "reviewing")["sha"] == nxt


def test_a_verdict_taken_first_is_not_timed_out(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    rid = only(env.p, "reviewing")["id"]
    env.clock.now += 1801
    env.p.verdict(rid, "pass", "ok", "reviewer")
    env.p.tick()
    assert env.p.get(rid)["state"] == "passed"


def test_restart_mid_deploy_needs_a_person(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    rid = only(env.p, "reviewing")["id"]
    env.p.runs[rid]["state"] = "deploying"
    env.p._save()
    p2 = Pipeline(env.s.state_dir, REPO, "unstable", ["nj"], env.forge, None, None, env.deployer, env.repo)
    assert p2.get(rid)["state"] == "needs-operator"


# ── over HTTP ────────────────────────────────────────────────────────────
def test_api_and_a_reviewers_token(env):
    m = JobManager(env.s.state_dir, lambda req, ev: {})
    g = GitOps(env.s, env.repo, m)
    g.runs = env.p
    review = Token("reviewer", _digest("r3view"), Scope(runs="review"))
    plain = Token("ci", _digest("c1"), Scope(estates=("nj",), infra="preview"))
    c = TestClient(create_app(m, env.s, "t0ken", g, [review, plain]))
    h, rh, ch = ({"Authorization": f"Bearer {t}"} for t in ("t0ken", "r3view", "c1"))
    sha = merge(env, 1, "two\n")
    assert c.post("/v1/runs", json={"repo": REPO, "sha": sha}).status_code == 401
    assert c.post("/v1/runs", json={"repo": REPO, "sha": sha}, headers=rh).status_code == 403
    assert c.post("/v1/runs", json={"repo": "other/repo", "sha": sha}, headers=h).status_code == 400
    assert c.post("/v1/runs", json={"repo": REPO, "sha": sha}, headers=h).json()["started"]
    assert c.get("/v1/runs", headers=ch).status_code == 403
    rid = c.get("/v1/runs?state=reviewing", headers=rh).json()[0]["id"]
    d = c.get(f"/v1/runs/{rid}/diff", headers=rh).json()
    assert "+two" in d["diff"] and not d["truncated"] and d["commits"] and d["sha"] == sha
    assert c.get(f"/v1/runs/{rid}/diff", headers=ch).status_code == 403
    # The reviewer's token cannot deploy.
    assert c.post("/v1/deploys", json={"estate": "nj"}, headers=rh).status_code == 403
    assert c.post(f"/v1/runs/{rid}/verdict", json={"verdict": "maybe", "evidence": ""}, headers=rh).status_code == 422
    r = c.post(f"/v1/runs/{rid}/verdict", json={"verdict": "pass", "evidence": "ok"}, headers=rh).json()
    assert r["state"] == "passed" and r["verdict"]["by"] == "reviewer"
    assert c.post(f"/v1/runs/{rid}/verdict", json={"verdict": "pass", "evidence": "ok"}, headers=rh).status_code == 409
    assert c.post(f"/v1/runs/{rid}/resolve", json={"note": "x"}, headers=rh).status_code == 403
    assert c.get("/v1/runs/nope", headers=h).status_code == 404


def test_webhook_merge_goes_through_the_guard(env):
    env.s.git.trigger, env.s.git.webhook_secret = "verified", "s3cret"
    m = JobManager(env.s.state_dir, lambda req, ev: {})
    g = GitOps(env.s, env.repo, m)
    g.runs = env.p
    c = TestClient(create_app(m, env.s, "t0ken", g))

    def hook(sha, n):
        body = json.dumps({"action": "closed", "repository": {"full_name": REPO}, "pull_request": {
            "number": n, "merged": True, "merge_commit_sha": sha, "base": {"ref": "unstable"},
            "head": {"sha": "x", "repo": {"full_name": REPO}}, "author_association": "OWNER"}}).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        return c.post("/v1/hooks/github", content=body,
                      headers={"x-github-event": "pull_request", "x-hub-signature-256": sig}).json()

    pushed = commit(env.origin, "pushed\n", "no PR")
    assert "not the merge commit" in hook(pushed, 5)["refused"]  # the payload is not believed
    assert hook(merge(env, 6, "two\n"), 6)["started"]


# ── adapters ─────────────────────────────────────────────────────────────
def test_github_checks_red_wins_and_pages(monkeypatch):
    gh = GitHub("t")
    runs = [{"id": i, "name": f"build {i}", "app": {"slug": "github-actions"}, "conclusion": "success"}
            for i in range(100)]
    runs += [{"id": 200, "name": "flake checks", "app": {"slug": "github-actions"}, "conclusion": "failure"},
             {"id": 300, "name": "flake checks", "app": {"slug": "other-app"}, "conclusion": "success"},
             {"id": 150, "name": "ticket-key", "app": {"slug": "github-actions"}, "conclusion": "failure"},
             {"id": 250, "name": "ticket-key", "app": {"slug": "github-actions"}, "conclusion": "success"}]

    def req(method, path, body=None):
        if "/status" in path:
            return {"statuses": [{"context": "legacy", "state": "success"}]}
        page = int(path.rsplit("page=", 1)[1])
        return {"check_runs": runs[(page - 1) * 100: page * 100]}

    monkeypatch.setattr(gh, "_req", req)
    out = gh.checks("acme/estate", "abc")
    assert out["flake checks"] == "failure"  # another app's later green does not hide it
    assert out["ticket-key"] == "success"  # a re-run in the same app does
    assert out["legacy"] == "success" and out["build 99"] == "success"


def test_github_token_file_is_read_for_every_request(tmp_path):
    f = tmp_path / "token"
    f.write_text("one\n")
    gh = GitHub("start", token_file=str(f))
    assert gh._token() == "one"
    f.write_text("two\n")  # the minter rewrote it
    assert gh._token() == "two"
    f.unlink()
    assert gh._token() == "start"
    assert GitHub("plain")._token() == "plain"


def test_pipeline_settings_take_a_protected_node_but_no_tag_or_glob(tmp_path, monkeypatch):
    s = Settings(flake=None, state_dir=tmp_path, passphrase="p",
                 git=GitSettings(url="x", branch="unstable", github_token="t", trigger="verified"))
    for k, v in {"FLEETKIT_REQUIRED_CHECKS": "flake checks", "FLEETKIT_GITHUB_REPO": REPO,
                 "FLEETKIT_PROTECT": "*/btc-mainnet*", "FLEETKIT_VERIFIED_ON": "web-1,btc-mainnet-1"}.items():
        monkeypatch.setenv(k, v)
    # The toplevel compare guards it now (runs.needs_operator).
    assert adapters.pipeline(s, None, None).nixos_on == ("web-1", "btc-mainnet-1")
    for sel in ("@verified", "btc-*", "*"):
        monkeypatch.setenv("FLEETKIT_VERIFIED_ON", sel)
        with pytest.raises(ValueError, match="not @tags or globs"):
            adapters.pipeline(s, None, None)
    monkeypatch.delenv("FLEETKIT_PROTECT")
    with pytest.raises(ValueError, match="FLEETKIT_PROTECT"):
        adapters.pipeline(s, None, None)


def test_exec_runner_passes_no_server_secrets(monkeypatch):
    monkeypatch.setenv("FLEETKIT_JIRA_TOKEN", "secret")
    monkeypatch.setenv("REVIEW_HOST", "h")
    r = adapters.ExecRunner("true", ("REVIEW_HOST",))
    assert "FLEETKIT_JIRA_TOKEN" not in r.env and r.env["REVIEW_HOST"] == "h"


class Flaky:
    """The fake deployer, failing the calls `fail` names (estate, preview) once each."""

    def __init__(self, env, *fail):
        self.real, self.fail = env.deployer.deploy, list(fail)

    def __call__(self, estate, sha, preview, on):
        out = self.real(estate, sha, preview, on)
        if (estate, preview) in self.fail:
            self.fail.remove((estate, preview))
            return {**out, "state": "failed", "error": "transient"}
        return out


def test_stable_never_moves_to_a_commit_some_estates_rolled_back_from(env, monkeypatch):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    monkeypatch.setattr(env.deployer, "deploy", Flaky(env, ("github", False)))
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    assert r["state"] == "needs-operator"  # nj is back at base, github is not
    with pytest.raises(ValueError):
        env.p.resolve(r["id"], "x", "operator", stable=True)
    assert env.forge.moved == []


def test_a_retry_of_a_retry_runs(env, monkeypatch):
    sha = merge(env, 1, "two\n")
    monkeypatch.setattr(env.deployer, "deploy", Flaky(env, ("github", False)))
    env.p.trigger(REPO, sha, "test")
    r1 = only(env.p, "needs-operator")  # nj deployed, github did not
    monkeypatch.setattr(env.deployer, "deploy", Flaky(env, ("nj", True)))
    env.p.resolve(r1["id"], "retry", "operator", accept=True)
    r2 = only(env.p, "needs-operator")  # its preview failed
    env.p.resolve(r2["id"], "retry again", "operator", accept=True)
    assert only(env.p, "reviewing")["sha"] == sha


def test_a_revert_closed_unmerged_leaves_the_ticket_in_the_next_review(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    env.p.resolve(r["id"], "revert closed; fixing forward", "operator")
    env.p.trigger(REPO, merge(env, 2, "fixed\n", "INFRA-2"), "test")
    assert only(env.p, "reviewing")["tickets"] == ["INFRA-1", "INFRA-2"]


def test_a_revert_merged_after_its_run_was_resolved_still_counts(env):
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    env.p.resolve(r["id"], "will merge the revert later", "operator")
    with pytest.raises(ValueError):
        env.p.resolve(r["id"], "x", "operator", accept=True)  # resolved: nothing to resolve
    rev = commit(env.origin, "one\n", "INFRA-1: Revert (squash)")
    env.forge.merge(rev, r["revert_pr"])
    later = merge(env, 3, "three\n", "INFRA-3")
    env.p.trigger(REPO, rev, "test")
    env.p.trigger(REPO, later, "test")
    run = only(env.p, "reviewing")
    assert run["tickets"] == ["INFRA-3"], run


def test_accept_does_not_rerun_a_failed_review_and_stable_does_not_take_it(env):
    env.tracker.labels["INFRA-1"] = ["no-auto-revert"]
    env.p.trigger(REPO, merge(env, 1, "two\n"), "test")
    r = env.p.verdict(only(env.p, "reviewing")["id"], "fail", "bad", "reviewer")
    with pytest.raises(ValueError, match="failed review"):
        env.p.resolve(r["id"], "x", "operator", stable=True)


def test_nixos_changes_reads_built_and_live_toplevels(tmp_path, monkeypatch):
    """nixos.changes against a fake colmena that prints what 0.4.0 prints."""
    fake = tmp_path / "colmena"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
open(os.environ["CALLS"], "a").write(json.dumps(a) + "\\n")
if a[0] == "eval":
    names = json.load(open(a[-1].split("builtins.readFile ")[1].split(")")[0].strip('"')))
    if "boom" in names:
        sys.exit("error: attribute missing")
    print(json.dumps({{n: {{"built": "/nix/store/b-system", "tags": ["mainnet"] if "btc" in n else []}}
                      for n in names if n in ("web-1", "btc-mainnet-1", "dup-3", "slow-4")}}))
elif "slow-4" in " ".join(a):
    sys.stderr.write("        slow-4 | /nix/store/a-system\\n")
    sys.stderr.flush()
    import time; time.sleep(30)
else:
    sys.stderr.write("[INFO ] Selected 3 nodes.\\n        web-1 | /nix/store/a-system\\n"
                     "btc-mainnet-1 | \\nbtc-mainnet-1 | Failed: ssh timed out\\n"
                     "        dup-3 | /nix/store/x-system\\n        dup-3 | /nix/store/y-system\\n"
                     "        other | /nix/store/z-system\\n")
    sys.exit(1)
""")
    fake.chmod(0o755)
    calls = tmp_path / "calls"
    monkeypatch.setenv("CALLS", str(calls))
    s = Settings(flake=tmp_path, state_dir=tmp_path / "state", passphrase="p", colmena=str(fake))
    events = []
    evil = "${throw (builtins.readFile /etc/hostname)}"
    out = nixos.changes(s, "nj", Emitter(events.append), ["web-1", "btc-mainnet-1", "dup-3", "gone-9", evil],
                        tmp_path)
    assert out == {"web-1": {"built": "/nix/store/b-system", "live": "/nix/store/a-system", "tags": []},
                   "btc-mainnet-1": {"built": "/nix/store/b-system", "live": None, "tags": ["mainnet"]},
                   "dup-3": {"built": "/nix/store/b-system", "live": None, "tags": []}}
    ev, ex = [json.loads(x) for x in calls.read_text().splitlines()]
    # The names are data in a file, never code in the expression.
    assert ev[0] == "eval" and "${throw" not in ev[-1] and "web-1" not in ev[-1]
    assert ex[:1] == ["exec"] and ex[ex.index("--on") + 1] == "web-1,btc-mainnet-1,dup-3"
    assert ex[-3:] == ["readlink", "-e", "/run/current-system"]
    assert [e["node"] for e in events if e["kind"] == "compare"] == ["web-1", "btc-mainnet-1", "dup-3"]
    with pytest.raises(nixos.NixosError, match="colmena eval"):
        nixos.changes(s, "nj", Emitter(events.append), ["boom"], tmp_path)
    # A node that answered before the timeout counts; one that did not stays unknown.
    out = nixos.changes(s, "nj", Emitter(events.append), ["slow-4", "web-1"], tmp_path, timeout=2)
    assert out["slow-4"]["live"] == "/nix/store/a-system" and out["web-1"]["live"] is None
