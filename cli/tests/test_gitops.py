"""GitOps with a local git repo as the remote and a fake runner: checkouts at
the commit, one submission per commit per estate, busy estates skipped, the
GitHub webhook's signature, and secret redaction. Offline."""
import hashlib
import hmac
import json
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fleetkit_cli.api import create_app
from fleetkit_cli.events import Emitter
from fleetkit_cli.gitops import GitOps, Repo
from fleetkit_cli.jobs import JobManager
from fleetkit_cli.settings import GitSettings, Settings

TOKEN = "t0ken"
H = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "hook-secret"


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, msg):
    (repo / "flake.nix").write_text(f"# {msg}\n{{ outputs = _: {{ }}; }}\n")
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", msg, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def env(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    first = commit(origin, "one")
    s = Settings(flake=None, state_dir=tmp_path / "state", passphrase="p",
                 git=GitSettings(url=str(origin), deploy_on_push=["homelab"], webhook_secret=SECRET))
    repo = Repo(s)
    gate = threading.Event()
    seen = []

    def runner(req, ev):
        sha = repo.resolve(req.rev)
        d = repo.checkout(sha)
        seen.append((req.estate, sha, (d / "flake.nix").read_text().splitlines()[0]))
        gate.wait(5)
        return {"rev": sha}

    m = JobManager(s.state_dir, runner)
    g = GitOps(s, repo, m)
    c = TestClient(create_app(m, s, TOKEN, g))
    return c, g, m, origin, first, gate, seen


def done(m):
    for _ in range(500):
        if not m.active:
            return
        time.sleep(0.01)
    raise AssertionError("jobs still running")


def test_sync_once_per_commit(env):
    c, g, m, origin, first, gate, seen = env
    gate.set()
    r = g.sync("test")
    assert r["head"] == first and list(r["submitted"]) == ["homelab"]
    done(m)
    assert seen == [("homelab", first, "# one")]
    assert g.sync("test")["skipped"]["homelab"] == "already submitted"
    second = commit(origin, "two")
    assert list(g.sync("test")["submitted"]) == ["homelab"]
    done(m)
    assert seen[-1] == ("homelab", second, "# two")
    assert c.get("/v1/gitops", headers=H).json()["submitted"]["homelab"]["rev"] == second


def test_busy_estate_is_skipped(env):
    c, g, m, origin, first, gate, seen = env
    g.sync("test")
    commit(origin, "two")
    r = g.sync("test")
    assert "deploying" in r["skipped"]["homelab"]
    gate.set()
    done(m)
    assert list(g.sync("test")["submitted"]) == ["homelab"]
    done(m)


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_webhook(env):
    c, g, m, origin, first, gate, seen = env
    gate.set()
    body = json.dumps({"ref": "refs/heads/main", "after": first}).encode()
    hdr = {"x-github-event": "push", "content-type": "application/json"}
    assert c.post("/v1/hooks/github", content=body, headers={**hdr, "x-hub-signature-256": "sha256=0"}).status_code == 401
    r = c.post("/v1/hooks/github", content=body, headers={**hdr, "x-hub-signature-256": sign(body)})
    assert r.status_code == 200 and list(r.json()["submitted"]) == ["homelab"]
    done(m)
    other = json.dumps({"ref": "refs/heads/feature", "after": first}).encode()
    r = c.post("/v1/hooks/github", content=other, headers={**hdr, "x-hub-signature-256": sign(other)})
    assert "ignored" in r.json()
    # The deploy API also takes a rev.
    jid = c.post("/v1/deploys", json={"estate": "homelab", "rev": first}, headers=H).json()["id"]
    done(m)
    assert c.get(f"/v1/deploys/{jid}", headers=H).json()["result"] == {"rev": first}


def test_redaction():
    out = []
    ev = Emitter(out.append)
    # Assembled here so no credential-shaped URL sits in the repo.
    url = "postgres://u" + ":" + "not-a-password" + "@" + "db/state"
    ev.secret(url)
    ev.emit("infra", "log", line=f"cannot open {url}: refused")
    assert out[0]["line"] == "cannot open [secret]: refused"


def test_only_commits_on_the_branch_deploy(tmp_path):
    from fleetkit_cli.gitops import make_runner
    from fleetkit_cli.pipeline import DeployRequest

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "stable", cwd=origin)
    on = commit(origin, "on stable")
    git("checkout", "-q", "-b", "unstable", cwd=origin)
    off = commit(origin, "only on unstable")
    s = Settings(flake=None, state_dir=tmp_path / "state", passphrase="p",
                 git=GitSettings(url=str(origin), branch="stable"))
    repo = Repo(s)
    runner = make_runner(s, repo, run=lambda s2, req, ev: {"at": (s2.flake / "flake.nix").read_text().splitlines()[0]})
    ev = Emitter(lambda e: None)
    assert runner(DeployRequest(estate="e", rev=on), ev) == {"rev": on, "at": "# on stable"}
    assert runner(DeployRequest(estate="e", rev=off, preview=True), ev)["rev"] == off
    with pytest.raises(PermissionError, match="is not on stable"):
        runner(DeployRequest(estate="e", rev=off), ev)
