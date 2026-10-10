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


# ── scoped tokens (tokens.py) ────────────────────────────────────────────
# A server with a repo (deploy branch main, preview branch unstable), the
# unscoped token and two scoped ones read from a tokens file: `tenant`
# (estate t: Pulumi previews, Colmena dry-activate and switch) by a file
# holding its value, `reader` (estate t, nothing may run) by its digest.
import hashlib  # noqa: E402
import subprocess  # noqa: E402

from click.testing import CliRunner  # noqa: E402

from fleetkit_cli import render, tokens  # noqa: E402
from fleetkit_cli.gitops import GitOps, Repo  # noqa: E402
from fleetkit_cli.settings import GitSettings, Settings  # noqa: E402

TENANT = "tenant-value-5d1f0c"
READER = "reader-value-b7e2a9"
TH = {"Authorization": f"Bearer {TENANT}"}
RH = {"Authorization": f"Bearer {READER}"}
VALUES = (TOKEN, TENANT, READER)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, msg):
    (repo / "flake.nix").write_text(f"# {msg}\n{{ outputs = _: {{ }}; }}\n")
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", msg, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def tokens_file(d, entries):
    d.mkdir(parents=True, exist_ok=True)
    f = d / "tokens.json"
    f.write_text(json.dumps({"tokens": entries}))
    return f


def tenant_tokens(d):
    d.mkdir(parents=True, exist_ok=True)
    (d / "tenant.token").write_text(TENANT + "\n")
    return tokens_file(d, {
        "tenant": {"file": str(d / "tenant.token"), "estates": ["t"], "infra": "preview", "nixos": "apply",
                   "goals": ["dry-activate", "switch"]},
        "reader": {"sha256": sha(READER), "estates": ["t"]},
    })


class Scoped:
    def __init__(self, tmp, monkeypatch):
        self.tmp = tmp
        origin = tmp / "origin"
        origin.mkdir()
        git("init", "-q", "-b", "main", cwd=origin)
        self.old = commit(origin, "one")
        self.head = commit(origin, "two")
        git("checkout", "-q", "-b", "unstable", cwd=origin)
        self.unstable = commit(origin, "on unstable only")
        git("checkout", "-q", "-b", "feature", cwd=origin)
        self.feature = commit(origin, "on feature only")
        git("checkout", "-q", "main", cwd=origin)
        self.origin = origin
        s = Settings(flake=None, state_dir=tmp / "state", passphrase="p",
                     git=GitSettings(url=str(origin), deploy_on_push=["ops"], preview_branches=["unstable"]))
        self.fake = Fake()
        self.m = JobManager(s.state_dir, self.fake)
        self.g = GitOps(s, Repo(s), self.m)
        monkeypatch.setattr(render, "estates", lambda s: {"ops": ["ops-guests"], "t": ["t-guests"]})
        self.c = TestClient(create_app(self.m, s, TOKEN, self.g, tokens.load(tenant_tokens(tmp / "conf"))))

    def jobs(self):
        """Every job there is, as the unscoped token sees them."""
        return self.c.get("/v1/deploys", headers=H).json()

    def finish(self):
        self.fake.gate.set()
        for _ in range(500):
            if not self.m.active:
                return
            time.sleep(0.01)
        raise AssertionError("jobs still running")


@pytest.fixture
def sc(tmp_path, monkeypatch):
    s = Scoped(tmp_path, monkeypatch)
    yield s
    s.fake.gate.set()


def refused(r, field):
    assert r.status_code == 403, (r.status_code, r.text)
    d = r.json()["detail"]
    assert d["field"] == field and d["error"].startswith("token tenant: "), d
    return d


# The Colmena stage alone, on the tenant's own hive: what `tenant` may apply.
OK = {"estate": "t", "infra": False, "goal": "dry-activate"}


@pytest.mark.parametrize("body, field", [
    ({**OK, "estate": "ops"}, "estate"),                          # another estate
    ({**OK, "hive": "ops"}, "hive"),                              # its estate, another estate's hive
    ({"estate": "t", "goal": "dry-activate"}, "infra"),           # infra (the default) without preview
    ({"estate": "t", "infra": True, "goal": "dry-activate"}, "infra"),
    ({**OK, "goal": "boot"}, "goal"),                             # a goal outside the list
    ({**OK, "goal": "test"}, "goal"),
    ({**OK, "preview": True, "goal": "boot"}, "goal"),            # also when the goal would not be used
    ({**OK, "allow_replace": ["web"]}, "allow_replace"),
    ({**OK, "allow_delete": ["web"]}, "allow_delete"),
    ({**OK, "allow_update": ["web"]}, "allow_update"),
    ({**OK, "allow_create": ["web"]}, "allow_create"),
    ({**OK, "targets": ["urn:x"]}, "targets"),
    ({**OK, "refresh": True}, "refresh"),
    ({**OK, "preview": True, "infra": True, "allow_delete": ["web"]}, "allow_delete"),
    ({**OK, "rev": "feature"}, "rev"),                            # a branch that is neither
    ({**OK, "rev": "no-such-rev"}, "rev"),
    ({**OK, "rev": "--all"}, "rev"),
    ({**OK, "rev": ""}, "rev"),
])
def test_scoped_refused_before_a_job_exists(sc, body, field):
    refused(sc.c.post("/v1/deploys", json=body, headers=TH), field)
    assert sc.jobs() == []
    assert list((sc.tmp / "state" / "jobs").iterdir()) == []


def test_scoped_rev_must_be_on_the_deploy_or_a_preview_branch(sc):
    for preview in (False, True):  # previews too: a preview evaluates the commit with credentials present
        body = {**OK, "preview": preview, "rev": sc.feature}
        d = refused(sc.c.post("/v1/deploys", json=body, headers=TH), "rev")
        assert sc.feature not in d["error"]
        # The same words for a commit that is not there at all.
        assert refused(sc.c.post("/v1/deploys", json={**body, "rev": "0" * 40}, headers=TH), "rev") == d
    assert sc.jobs() == []
    # The unscoped token is not held to it (the runner's own gate is unchanged).
    r = sc.c.post("/v1/deploys", json={"estate": "t", "preview": True, "rev": sc.feature}, headers=H)
    assert r.status_code == 202 and r.json()["request"]["rev"] == sc.feature and r.json()["by"] == "api-token"


def test_scoped_cannot_read_or_cancel_another_estates_job(sc):
    ops = sc.c.post("/v1/deploys", json={"estate": "ops"}, headers=H).json()["id"]
    wait_state(sc.c, ops, "running")
    missing = sc.c.get("/v1/deploys/nope", headers=TH)
    assert missing.status_code == 404
    for method, path in (("get", ""), ("get", "/events"), ("get", "/stream"), ("post", "/cancel")):
        r = getattr(sc.c, method)(f"/v1/deploys/{ops}{path}", headers=TH)
        # Exactly what an id that never was a job gets: ids do not leak.
        assert r.status_code == 404 and r.json() == {"detail": f"no job {ops}"}, (path, r.status_code, r.text)
        assert r.json()["detail"].replace(ops, "nope") == missing.json()["detail"]
    assert sc.c.get("/v1/deploys", headers=TH).json() == []
    # Not cancelled.
    assert sc.c.get(f"/v1/deploys/{ops}", headers=H).json()["state"] == "running"
    # Its own estate's jobs, whoever started them: listed, read, followed, cancelled.
    own = sc.c.post("/v1/deploys", json={"estate": "t"}, headers=H).json()["id"]
    wait_state(sc.c, own, "running")
    assert [j["id"] for j in sc.c.get("/v1/deploys", headers=TH).json()] == [own]
    assert [j["id"] for j in sc.c.get("/v1/deploys?limit=1", headers=TH).json()] == [own]
    assert sc.c.get(f"/v1/deploys/{own}", headers=TH).json()["by"] == "api-token"
    assert sc.c.get(f"/v1/deploys/{own}/events", headers=TH).json()["events"][0]["kind"] == "step"
    assert sc.c.post(f"/v1/deploys/{own}/cancel", headers=TH).status_code == 200
    wait_state(sc.c, own, "cancelled")
    assert "event: end" in sc.c.get(f"/v1/deploys/{own}/stream", headers=TH).text


def test_scoped_gitops_routes_refused(sc):
    for r in (sc.c.post("/v1/gitops/sync", headers=TH), sc.c.get("/v1/gitops", headers=TH)):
        assert r.status_code == 403 and r.json()["detail"]["field"] is None
        assert "GitOps" in r.json()["detail"]["error"]
    assert sc.jobs() == []
    assert sc.c.get("/v1/gitops", headers=H).status_code == 200
    r = sc.c.post("/v1/gitops/sync", headers=H)
    assert r.status_code == 200 and list(r.json()["submitted"]) == ["ops"]
    assert sc.jobs()[0]["by"] == "gitops"


def test_scoped_estates_filtered(sc):
    assert sc.c.get("/v1/estates", headers=H).json() == {"ops": ["ops-guests"], "t": ["t-guests"]}
    assert sc.c.get("/v1/estates", headers=TH).json() == {"t": ["t-guests"]}


def test_scoped_can_preview_and_apply_its_own(sc):
    def run(body):
        r = sc.c.post("/v1/deploys", json=body, headers=TH)
        assert r.status_code == 202, r.text
        rec = r.json()
        assert rec["by"] == "tenant"
        sc.finish()
        assert wait_state(sc.c, rec["id"], "succeeded")["by"] == "tenant"
        return rec["request"]

    # A preview of its estate: pulumi preview and colmena build, at the head.
    req = run({"estate": "t", "preview": True})
    assert req["infra"] and req["nixos"] and req["preview"]
    # The commit that was checked is the commit that runs.
    assert req["rev"] == sc.head
    assert run({"estate": "t", "preview": True, "rev": "main", "stacks": ["t-guests"], "pr": 7})["rev"] == sc.head
    # A commit on a preview branch.
    assert run({"estate": "t", "preview": True, "rev": sc.unstable})["rev"] == sc.unstable
    assert run({"estate": "t", "preview": True, "rev": "unstable"})["rev"] == sc.unstable
    # dry-activate and switch of named hosts of its hive, at a commit on the deploy branch.
    for goal in ("dry-activate", "switch"):
        req = run({"estate": "t", "infra": False, "hive": "t", "on": ["web", "@db"], "goal": goal, "rev": sc.old})
        assert (req["goal"], req["on"], req["rev"], req["infra"]) == (goal, ["web", "@db"], sc.old, False)
    assert [j["by"] for j in sc.jobs()] == ["tenant"] * 6
    # One deploy per estate holds for it as for anyone.
    sc.fake.gate.clear()
    a = sc.c.post("/v1/deploys", json=OK, headers=TH).json()["id"]
    r = sc.c.post("/v1/deploys", json=OK, headers=TH)
    assert r.status_code == 409 and r.json()["detail"]["job"] == a


def test_scoped_defaults_allow_nothing(sc):
    """`reader`: estates only. No stage, no goal but dry-activate, no rev off the branches."""
    def field(body):
        r = sc.c.post("/v1/deploys", json=body, headers=RH)
        assert r.status_code == 403, r.text
        return r.json()["detail"]["field"]

    assert field({"estate": "t"}) == "infra"
    assert field({"estate": "t", "preview": True}) == "infra"
    assert field({"estate": "t", "infra": False}) == "nixos"
    assert field({"estate": "t", "infra": False, "preview": True}) == "nixos"
    assert field({"estate": "t", "infra": False, "nixos": False, "stacks": ["t-guests"]}) == "stacks"
    assert field({"estate": "t", "infra": False, "nixos": False, "on": ["web"]}) == "on"
    assert field({"estate": "t", "infra": False, "nixos": False, "hive": "ops"}) == "hive"
    assert field({"estate": "t", "infra": False, "nixos": False, "goal": "test"}) == "goal"
    assert field({"estate": "t", "infra": False, "nixos": False, "rev": sc.feature}) == "rev"
    assert sc.jobs() == []
    assert sc.c.get("/v1/estates", headers=RH).json() == {"t": ["t-guests"]}


def test_hives_and_revs_of_a_scope(tmp_path):
    f = tokens_file(tmp_path, {
        "a": {"sha256": sha("a-value"), "estates": ["t"], "hives": ["shared"], "nixos": "apply", "revs": "any",
              "allow": True, "infra": "apply"},
        "b": {"sha256": sha("b-value"), "estates": ["t"], "nixos": "build"},
    })
    a, b = (tokens.Principal(t.name, t.scope) for t in tokens.load(f))
    never = lambda rev: None  # noqa: E731 - no rev is on a branch
    # hives: the request's hive defaults to its estate, which is not in the list.
    with pytest.raises(tokens.Refused) as e:
        tokens.check(a, DeployRequest(estate="t", goal="dry-activate"), never)
    assert e.value.field == "hive"
    # revs = any: not asked; allow: the guard's names, targets and refresh.
    req = DeployRequest(estate="t", hive="shared", goal="dry-activate", rev="anything", refresh=True,
                        targets=["urn:x"], allow_delete=["old"])
    assert tokens.check(a, req, never) is req
    # nixos = build: the Colmena stage in a preview only.
    with pytest.raises(tokens.Refused) as e:
        tokens.check(b, DeployRequest(estate="t", infra=False, goal="dry-activate"), never)
    assert e.value.field == "nixos"
    assert tokens.check(b, DeployRequest(estate="t", infra=False, preview=True), lambda rev: "abc").rev == "abc"
    # A server without a repo deploys its working tree: no rev to choose.
    with pytest.raises(tokens.Refused) as e:
        tokens.check(b, DeployRequest(estate="t", infra=False, preview=True, rev="main"))
    assert e.value.field == "rev"
    assert tokens.check(b, DeployRequest(estate="t", infra=False, preview=True)).rev is None


def test_every_request_field_is_classified():
    """A field added to DeployRequest must be classified in tokens.py; until
    it is, a scoped token may not set it."""
    assert set(DeployRequest.model_fields) == set(tokens.CLASSIFIED)

    class Later(DeployRequest):
        force: bool = False
        notes: list[str] = []

    p = tokens.Principal("tenant", tokens.Scope(estates=("t",), hives=("t",), nixos="apply"))
    ok = dict(estate="t", infra=False, goal="dry-activate")
    assert tokens.check(p, Later(**ok)).estate == "t"
    for extra in ({"force": True}, {"notes": ["x"]}):
        with pytest.raises(tokens.Refused) as e:
            tokens.check(p, Later(**ok, **extra))
        assert e.value.field == next(iter(extra))
    # The unscoped token is not checked at all.
    assert tokens.check(tokens.Principal(tokens.UNSCOPED), Later(estate="x", force=True)).force


def test_no_token_or_digest_in_jobs_events_or_errors(sc, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    seen = []
    for h in (H, TH, RH, {"Authorization": "Bearer nope"}):
        for r in (sc.c.post("/v1/deploys", json=OK, headers=h), sc.c.post("/v1/deploys", json={"estate": "ops"}, headers=h),
                  sc.c.get("/v1/deploys", headers=h), sc.c.get("/v1/gitops", headers=h),
                  sc.c.get("/v1/deploys/nope", headers=h)):
            seen.append(r.text)
        sc.finish()
        sc.fake.gate.clear()
    for j in sc.jobs():
        seen.append(sc.c.get(f"/v1/deploys/{j['id']}/stream", headers=H).text)
        sc.finish()
    assert {j["by"] for j in sc.jobs()} == {"api-token", "tenant"}
    seen += [p.read_text() for p in (sc.tmp / "state").rglob("*") if p.is_file() and ".git" not in p.parts
             and "repo.git" not in p.parts]
    seen.append(caplog.text)
    seen.append(repr(tokens.load(sc.tmp / "conf" / "tokens.json")))
    blob = "\n".join(seen)
    for v in VALUES:
        assert v not in blob
        assert sha(v) not in blob and sha(v).upper() not in blob


def test_tokens_are_named_by_digest_or_file(sc):
    assert sc.c.get("/v1/deploys", headers=TH).status_code == 200   # a file holding the value
    assert sc.c.get("/v1/deploys", headers=RH).status_code == 200   # a digest
    for bad in (TENANT + "x", TENANT[:-1], sha(TENANT), sha(READER), "", " " + TENANT):
        assert sc.c.get("/v1/deploys", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    for header in (TENANT, f"bearer {TENANT}", f"Bearer  {TENANT}", f"Basic {TENANT}"):
        assert sc.c.get("/v1/deploys", headers={"Authorization": header}).status_code == 401
    assert sc.c.get("/v1/deploys").status_code == 401


def test_every_digest_is_compared_whichever_matches(tmp_path, monkeypatch):
    f = tokens_file(tmp_path, {n: {"sha256": sha(n + "-value"), "estates": [n]} for n in ("a", "b", "c")})
    auth = tokens.Authenticator(TOKEN, tokens.load(f))
    calls = []
    real = tokens.hmac.compare_digest

    def counting(x, y):
        calls.append(len(x) == len(y) == 32)
        return real(x, y)

    monkeypatch.setattr(tokens.hmac, "compare_digest", counting)
    for value, name in ((TOKEN, "api-token"), ("a-value", "a"), ("c-value", "c"), ("nope", None)):
        calls.clear()
        p = auth.authenticate(f"Bearer {value}")
        assert (p.name if p else None) == name
        assert calls == [True] * 4   # all four, digests against a digest, no early exit
    calls.clear()
    assert auth.authenticate("") is None and len(calls) == 4
    assert auth.authenticate(f"Bearer {TOKEN}").scope is None
    assert auth.authenticate("Bearer a-value").scope.hives == ("a",)   # hives default to the estates
    assert auth.authenticate("Bearer a-value").scope.goals == ("dry-activate",)


def _bad_files(d):
    """name -> (the tokens file's text, a word its error must have)."""
    good = {"sha256": sha("x-value"), "estates": ["t"]}
    (d / "empty.token").write_text("\n")
    (d / "same.token").write_text("x-value")
    one = lambda e: json.dumps({"tokens": {"x": e}})  # noqa: E731
    return {
        "not json": ("{tokens: ", "not JSON"),
        "truncated": (one(good)[:-9], "not JSON"),
        "a list": ("[]", "must be"),
        "no tokens key": (json.dumps({"token": {}}), "must be"),
        "tokens a list": (json.dumps({"tokens": [good]}), "must be"),
        "entry a string": (one("x"), "must be an object"),
        "unknown key": (one({**good, "estate": "t"}), "unknown key estate"),
        "no value": (one({"estates": ["t"]}), "exactly one of"),
        "both": (one({**good, "file": str(d / "same.token")}), "exactly one of"),
        "short digest": (one({**good, "sha256": "abc"}), "64 hex digits"),
        "digest not hex": (one({**good, "sha256": "z" * 64}), "64 hex digits"),
        "value file missing": (one({"file": str(d / "missing.token"), "estates": ["t"]}), "cannot read"),
        "value file a directory": (one({"file": str(d), "estates": ["t"]}), "cannot read"),
        "value file empty": (one({"file": str(d / "empty.token"), "estates": ["t"]}), "is empty"),
        "bad infra": (one({**good, "infra": "yes"}), "infra must be one of"),
        "bad nixos": (one({**good, "nixos": "preview"}), "nixos must be one of"),
        "bad goal": (one({**good, "goals": ["switch", "yolo"]}), "goals has yolo"),
        "bad revs": (one({**good, "revs": "branch"}), "revs must be one of"),
        "allow not a bool": (one({**good, "allow": "yes"}), "allow must be"),
        "estates a string": (one({**good, "estates": "t"}), "estates must be a list"),
        "reserved name": (json.dumps({"tokens": {"api-token": good}}), "reserved"),
        "bad name": (json.dumps({"tokens": {"a b": good}}), "token name"),
        "a name twice": ('{"tokens": {"x": %s, "x": %s}}' % (json.dumps(good), json.dumps({**good, "sha256": sha("y")})),
                         "given twice"),
    }


def test_a_bad_tokens_file_stops_the_server(tmp_path):
    from fleetkit_cli.main import cli

    def serve(path, **env):
        return CliRunner().invoke(cli, ["serve", "--tokens-file", str(path)], env={"FLEETKIT_API_TOKEN": TOKEN, **env})

    cases = _bad_files(tmp_path)
    for name, (text, word) in cases.items():
        f = tmp_path / "tokens.json"
        f.write_text(text)
        with pytest.raises(tokens.TokenError, match=word):
            tokens.load(f)
        r = serve(f)
        # Not started: an error naming the file, never a server with fewer tokens.
        assert r.exit_code != 0 and str(f) in r.output and word in r.output, (name, r.output)
        assert "x-value" not in r.output and sha("x-value") not in r.output, name
    r = serve(tmp_path / "nowhere.json")
    assert r.exit_code != 0 and "cannot read" in r.output
    # Two names for one value, in the file or against the unscoped token.
    same = tokens_file(tmp_path, {"x": {"sha256": sha("x-value"), "estates": ["t"]},
                                  "y": {"file": "same.token", "estates": ["u"]}})
    r = serve(same)
    assert r.exit_code != 0 and "tokens x and y have the same value" in r.output and "x-value" not in r.output
    with pytest.raises(tokens.TokenError, match="x and y have the same value"):
        tokens.Authenticator(None, tokens.load(same))
    r = serve(tokens_file(tmp_path, {"x": {"sha256": sha(TOKEN), "estates": ["t"]}}))
    assert r.exit_code != 0 and "tokens api-token and x have the same value" in r.output
    # No auth and scopes together mean nothing.
    ok = tokens_file(tmp_path, {"x": {"sha256": sha("x-value"), "estates": ["t"]}})
    r = CliRunner().invoke(cli, ["serve", "--no-auth", "--tokens-file", str(ok)])
    assert r.exit_code != 0 and "--no-auth and a tokens file" in r.output
    # A file with no token in it is no token.
    r = CliRunner().invoke(cli, ["serve", "--tokens-file", str(tokens_file(tmp_path, {}))], env={"FLEETKIT_API_TOKEN": ""})
    assert r.exit_code != 0 and "no token" in r.output


def test_scoped_tokens_alone(tmp_path):
    """A server with a tokens file and no unscoped token still asks for one of them."""
    f = tokens_file(tmp_path, {"x": {"sha256": sha("x-value"), "estates": ["t"], "nixos": "apply"}})
    fake = Fake()
    fake.gate.set()
    c = TestClient(create_app(JobManager(tmp_path, fake), None, None, None, tokens.load(f)))
    assert c.get("/v1/deploys").status_code == 401
    assert c.get("/v1/deploys", headers=H).status_code == 401
    x = {"Authorization": "Bearer x-value"}
    r = c.post("/v1/deploys", json=OK, headers=x)
    assert r.status_code == 202 and r.json()["by"] == "x" and r.json()["request"]["rev"] is None
    assert c.post("/v1/deploys", json={**OK, "rev": "main"}, headers=x).json()["detail"]["field"] == "rev"


def test_no_auth_records_who(tmp_path):
    fake = Fake()
    fake.gate.set()
    c = TestClient(create_app(JobManager(tmp_path, fake), None, None))
    assert c.post("/v1/deploys", json={"estate": "mini"}).json()["by"] == "no-auth"


def test_by_survives_a_restart_and_old_records_load(env):
    c, fake, m, tmp = env
    jid = c.post("/v1/deploys", json={"estate": "mini"}, headers=H).json()["id"]
    fake.gate.set()
    assert wait_state(c, jid, "succeeded")["by"] == "api-token"
    f = tmp / "jobs" / f"{jid}.json"
    assert json.loads(f.read_text())["by"] == "api-token"
    assert JobManager(tmp, Fake()).jobs[jid].by == "api-token"
    # A record written before `by` existed.
    old = json.loads(f.read_text())
    del old["by"]
    f.write_text(json.dumps(old))
    assert JobManager(tmp, Fake()).jobs[jid].record()["by"] is None


def test_openapi_shows_the_refusal(env):
    c, *_ = env
    doc = c.get("/openapi.json").json()
    r = doc["paths"]["/v1/deploys"]["post"]["responses"]["403"]
    assert "scope" in r["description"] and "field" in r["description"]
    ref = r["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    detail = doc["components"]["schemas"][ref]["properties"]["detail"]["$ref"].rsplit("/", 1)[-1]
    assert set(doc["components"]["schemas"][detail]["properties"]) == {"error", "field"}


def test_the_nixos_modules_file_loads(tmp_path):
    """tokens-module.json is what nixosModules.fleetkit-server renders
    (tests/fleetkit_server.sh holds the module to it): the server reads it."""
    from pathlib import Path

    doc = json.loads((Path(__file__).parent / "tokens-module.json").read_text())
    for name, e in doc["tokens"].items():
        if "file" in e:
            assert e["file"].startswith("/run/secrets/")   # a path, never the value
            (tmp_path / name).write_text(f"{name}-value\n")
            e["file"] = str(tmp_path / name)
    by_name = {t.name: t.scope for t in tokens.load(tokens_file(tmp_path, doc["tokens"]))}
    assert by_name["tenant-ci"] == tokens.Scope(estates=("tenant",), hives=("tenant",), infra="preview",
                                                nixos="apply", goals=("dry-activate", "switch"))
    # The module's defaults are the server's.
    assert by_name["reader"] == tokens.Scope(estates=("tenant",), hives=("tenant",))
    assert by_name["operator-ci"] == tokens.Scope(
        estates=("homelab", "tenant"), hives=("homelab", "tenant", "nodes"), infra="apply", nixos="apply",
        goals=("switch", "test", "boot", "dry-activate"), revs="any", allow=True)
    assert set(by_name["operator-ci"].goals) == set(tokens.GOALS)


def test_scoped_gets_no_words_of_git_or_of_the_evaluation(sc, monkeypatch):
    import shutil

    def broken(s):
        raise RuntimeError(f"error: attribute 'ops-secret' missing at {sc.origin}/flake.nix")

    monkeypatch.setattr(render, "estates", broken)
    assert "ops-secret" in sc.c.get("/v1/estates", headers=H).json()["detail"]
    r = sc.c.get("/v1/estates", headers=TH)
    assert r.status_code == 500 and r.json() == {"detail": "the estates could not be evaluated"}
    # The repo cannot be fetched: the rev was not checked, so nothing runs.
    shutil.rmtree(sc.origin)
    r = sc.c.post("/v1/deploys", json=OK, headers=TH)
    assert r.status_code == 502 and str(sc.origin) not in r.text and "git" not in r.text
    assert sc.jobs() == []
