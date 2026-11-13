"""Pull requests with a local git repo (exposing refs/pull/<n>/head as GitHub
does) and a fake GitHub API: previews of trusted PRs at their head, statuses
and one edited comment per estate, forks and untrusted authors not previewed,
deploys on merge with FLEETKIT_TRIGGER=pr, and a busy estate's preview queued
rather than lost. Offline."""
import hashlib
import hmac
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from fleetkit_cli.api import create_app
from fleetkit_cli.gitops import GitOps, Repo
from fleetkit_cli.jobs import JobManager
from fleetkit_cli.settings import GitSettings, Settings

SECRET = "hook-secret"
REPO = "example/estate"


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, msg):
    (repo / "flake.nix").write_text(f"# {msg}\n")
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", msg, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


class FakeGitHub:
    def __init__(self):
        self.statuses, self.comments, self.lock = [], {}, threading.Lock()
        gh = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                return json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"null")

            def do_GET(self):
                pr = int(self.path.split("/issues/")[1].split("/")[0])
                with gh.lock:
                    self._send(200, [c for c in gh.comments.values() if c["pr"] == pr])

            def do_POST(self):
                b = self._body()
                with gh.lock:
                    if "/statuses/" in self.path:
                        gh.statuses.append({"sha": self.path.rsplit("/", 1)[1], **b})
                    else:
                        pr = int(self.path.split("/issues/")[1].split("/")[0])
                        cid = len(gh.comments) + 1
                        gh.comments[cid] = {"id": cid, "pr": pr, "body": b["body"]}
                self._send(201, {})

            def do_PATCH(self):
                b = self._body()
                with gh.lock:
                    gh.comments[int(self.path.rsplit("/", 1)[1])]["body"] = b["body"]
                self._send(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def env(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    base = commit(origin, "base")
    gh = FakeGitHub()
    gate = threading.Event()
    gate.set()
    ran = []

    def make(trigger="push"):
        s = Settings(flake=None, state_dir=tmp_path / f"state-{trigger}", passphrase="p",
                     git=GitSettings(url=str(origin), deploy_on_push=["homelab"], webhook_secret=SECRET,
                                     trigger=trigger, github_token="gh-token", github_api=gh.url,
                                     public_url="https://deploy.example.com"))
        repo = Repo(s)

        def runner(req, ev):
            sha = repo.resolve(req.rev)
            repo.checkout(sha)
            ran.append({"rev": sha, "pr": req.pr, "preview": req.preview})
            gate.wait(5)
            return {"rev": sha, "infra": {"homelab-guests": {"create": 1, "same": 5}},
                    "programs": {"homelab-guests": "/nix/store/abc-Pulumi.yaml"},
                    "nixos": "built" if req.preview else req.goal}

        m = JobManager(s.state_dir, runner)
        g = GitOps(s, repo, m)
        return TestClient(create_app(m, s, "t", g)), m

    yield origin, base, gh, gate, ran, make
    gh.server.shutdown()


def open_pr(origin, n, msg):
    git("checkout", "-q", "-B", f"pr{n}", cwd=origin)
    sha = commit(origin, msg)
    git("update-ref", f"refs/pull/{n}/head", sha, cwd=origin)
    git("checkout", "-q", "main", cwd=origin)
    return sha


def pr_event(action, n, sha, *, fork=False, assoc="MEMBER", merged=False, merge_sha=None, draft=False, base="main"):
    return {
        "action": action,
        "repository": {"full_name": REPO},
        "pull_request": {
            "number": n, "draft": draft, "merged": merged, "merge_commit_sha": merge_sha,
            "author_association": assoc, "base": {"ref": base},
            "head": {"sha": sha, "repo": {"full_name": "someone/fork" if fork else REPO}},
        },
    }


def hook(c, payload, event="pull_request"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return c.post("/v1/hooks/github", content=body,
                  headers={"x-github-event": event, "x-hub-signature-256": sig, "content-type": "application/json"})


def settle(m, gh, n_status):
    for _ in range(500):
        if not m.active and len(gh.statuses) >= n_status:
            time.sleep(0.05)
            return
        time.sleep(0.01)
    raise AssertionError(f"not settled: active={m.active} statuses={gh.statuses}")


def test_trusted_pr_previewed_and_reported(env):
    origin, base, gh, gate, ran, make = env
    c, m = make()
    sha = open_pr(origin, 7, "feature")
    r = hook(c, pr_event("opened", 7, sha)).json()
    assert r["kind"] == "preview" and r["sha"] == sha
    settle(m, gh, 2)
    assert ran == [{"rev": sha, "pr": 7, "preview": True}]
    assert [(s["sha"], s["state"], s["context"]) for s in gh.statuses] == [
        (sha, "pending", "fleetkit/homelab"), (sha, "success", "fleetkit/homelab")]
    assert gh.statuses[1]["description"] == "preview succeeded: homelab-guests: create 1"
    assert gh.statuses[1]["target_url"] == f"https://deploy.example.com/v1/deploys/{r['submitted']['homelab']}"
    (c1,) = gh.comments.values()
    assert c1["pr"] == 7 and "<!-- fleetkit:homelab -->" in c1["body"] and sha[:10] in c1["body"]
    assert "| `homelab-guests` | 1 | 0 | 0 | 0 | 5 | `abc-Pulumi.yaml` |" in c1["body"]
    assert "Merging deploys this" not in c1["body"]  # push trigger: the push deploys
    # A new push to the PR: a new preview, the same comment edited.
    git("checkout", "-q", "pr7", cwd=origin)
    sha2 = commit(origin, "feature 2")
    git("update-ref", "refs/pull/7/head", sha2, cwd=origin)
    git("checkout", "-q", "main", cwd=origin)
    hook(c, pr_event("synchronize", 7, sha2))
    settle(m, gh, 4)
    assert len(gh.comments) == 1 and sha2[:10] in gh.comments[1]["body"]


def test_untrusted_prs_not_previewed(env):
    origin, base, gh, gate, ran, make = env
    c, m = make()
    sha = open_pr(origin, 8, "from a fork")
    assert "untrusted" in hook(c, pr_event("opened", 8, sha, fork=True)).json()["ignored"]
    assert "untrusted" in hook(c, pr_event("opened", 8, sha, assoc="CONTRIBUTOR")).json()["ignored"]
    assert hook(c, pr_event("opened", 8, sha, draft=True)).json()["ignored"] == "draft"
    assert hook(c, pr_event("opened", 8, sha, base="dev")).json()["ignored"] == "base dev"
    time.sleep(0.2)
    assert ran == [] and gh.comments == {}
    assert [s["state"] for s in gh.statuses] == ["error", "error"]
    assert gh.statuses[0]["description"] == "not previewed: from a fork"


def test_trigger_pr_deploys_on_merge_only(env):
    origin, base, gh, gate, ran, make = env
    c, m = make("pr")
    sha = open_pr(origin, 9, "feature")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "merge", "-q", "--no-ff", "-m", "merge", "pr9", cwd=origin)
    merge = git("rev-parse", "HEAD", cwd=origin)
    assert "ignored" in hook(c, {"ref": "refs/heads/main", "after": merge}, event="push").json()
    hook(c, pr_event("opened", 9, sha))
    settle(m, gh, 2)
    assert "Merging deploys this" in gh.comments[1]["body"]
    r = hook(c, pr_event("closed", 9, sha, merged=True, merge_sha=merge)).json()
    assert r["kind"] == "deploy" and r["sha"] == merge
    settle(m, gh, 4)
    assert ran[-1] == {"rev": merge, "pr": 9, "preview": False}
    assert gh.statuses[-1]["description"].startswith("deploy succeeded")
    assert "fleetkit deploy" in gh.comments[1]["body"] and "colmena apply switch" in gh.comments[1]["body"]


def test_busy_estate_queues_the_preview(env):
    origin, base, gh, gate, ran, make = env
    c, m = make()
    gate.clear()
    a = open_pr(origin, 10, "a")
    b = open_pr(origin, 11, "b")
    hook(c, pr_event("opened", 10, a))
    assert hook(c, pr_event("opened", 11, b)).json()["submitted"]["homelab"] == "queued"
    gate.set()
    settle(m, gh, 4)
    for _ in range(200):
        if len(ran) == 2 and not m.active:
            break
        time.sleep(0.01)
    assert [x["pr"] for x in ran] == [10, 11]
