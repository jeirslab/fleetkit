"""Secret roots an estate's flake declares (`secretRoots` of a stack, as
`render.stacks` evaluates it at a checkout): a stack's sops files are looked
up in the checkout, then in the declared roots, then in FLEETKIT_SECRET_ROOTS;
a declared root that is not on disk fails the render by name, before anything
runs; a GitOps job reads the roots of its own commit. With the fake engine,
and once with the real one (offline). What reads a real flake input is
tests/pulumi_secret_roots.sh."""
import json
import os
import subprocess
from dataclasses import replace

import pytest

import fakes
from fakes import POOL
from fleetkit_cli import adopt, backends, pipeline, render
from fleetkit_cli.events import Emitter
from fleetkit_cli.gitops import Repo, make_runner
from fleetkit_cli.pipeline import DeployRequest
from fleetkit_cli.settings import GitSettings, Settings

SECRET = "secrets/tenant.yaml"


def root(tmp_path, name, text=None):
    """A directory with the secrets file (text: its content), or without it."""
    d = tmp_path / name
    (d / "secrets").mkdir(parents=True)
    if text is not None:
        (d / SECRET).write_text(text)
    return d


def declare(estate, roots, secrets=(SECRET,), backend=None):
    w = estate.stack(fakes.program(apps={"type": POOL, "properties": {"poolId": "apps"}}))
    st = estate.stacks["mini-guests"]
    st["secretRoots"], st["secrets"] = [str(r) for r in roots], list(secrets)
    if backend:
        st["backend"] = backend
    return w, st


def deploy(estate, events=None, **req):
    ev = Emitter((events if events is not None else []).append)
    return pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False, **req), ev)


def linked(world):
    """Where the secrets file of the engine's project dir pointed, per call."""
    return [c["secret"] for c in world.calls]


@pytest.fixture
def seen(estate, monkeypatch):
    """Each engine call records what its project dir's secrets file is."""
    stack = fakes.World.stack

    def recording(self, wd):
        s = stack(self, wd)
        for verb in ("preview", "up"):
            def call(*a, _f=getattr(s, verb), **k):
                r = _f(*a, **k)
                self.calls[-1]["secret"] = os.path.realpath(wd / SECRET)
                return r
            setattr(s, verb, call)
        return s
    monkeypatch.setattr(fakes.World, "stack", recording)
    return estate


def test_the_view_reads_declared_roots_with_a_default(monkeypatch):
    assert "secretRoots = x.secretRoots or [ ]" in render.VIEW
    base = {"estate": "mini", "project": "p", "backend": None, "file": "/f", "secrets": []}
    monkeypatch.setattr(render, "nix_eval_json", lambda s, attr, apply=None: {
        "old": dict(base), "new": {**base, "secretRoots": ["/nix/store/x-source"]}})
    out = render.stacks(None)
    assert out["old"]["secretRoots"] == [] and out["new"]["secretRoots"] == ["/nix/store/x-source"]


def test_a_file_under_a_declared_root_is_what_the_engine_reads(seen, tmp_path):
    tenant = root(tmp_path, "tenant", "t")
    w, _ = declare(seen, [tenant])
    assert deploy(seen)["applied"] == ["mini-guests"]
    assert linked(w) == [str(tenant / SECRET)] * 2  # the preview and the up


def test_an_estate_that_declares_none_looks_where_it_did(estate, tmp_path):
    """No `secretRoots` at all (a stack as an older kit evaluates it): the
    checkout, then the environment's roots; nothing else."""
    extra = root(tmp_path, "extra", "x")
    s = replace(estate.s, extra_secret_roots=[extra])
    for st in ({}, {"secretRoots": []}, None):
        assert render.secret_roots(s, st) == [estate.repo, extra]
    assert render.find_secret(s, SECRET) == extra / SECRET
    with pytest.raises(render.RenderError, match="secrets file secrets/tenant.yaml is under none of"):
        render.find_secret(estate.s, SECRET, {"secretRoots": []})


def test_the_order_is_checkout_then_declared_then_environment(estate, tmp_path):
    a, b, env = root(tmp_path, "a", "a"), root(tmp_path, "b", "b"), root(tmp_path, "env", "e")
    s = replace(estate.s, extra_secret_roots=[env])
    st = {"secretRoots": [str(a), str(b)]}
    assert render.secret_roots(s, st) == [estate.repo, a, b, env]
    assert render.find_secret(s, SECRET, st) == a / SECRET
    (a / SECRET).unlink()
    assert render.find_secret(s, SECRET, st) == b / SECRET
    (estate.repo / "secrets").mkdir()
    (estate.repo / SECRET).write_text("own")
    assert render.find_secret(s, SECRET, st) == estate.repo / SECRET


def test_the_environments_roots_are_still_added(seen, tmp_path):
    """FLEETKIT_SECRET_ROOTS, with roots declared too: a file only it has."""
    tenant, env = root(tmp_path, "tenant"), root(tmp_path, "env", "e")
    w, _ = declare(seen, [tenant])
    seen.s.extra_secret_roots = [env]
    assert deploy(seen)["applied"] == ["mini-guests"]
    assert linked(w) == [str(env / SECRET)] * 2


def test_from_env_reads_the_environments_roots(estate, monkeypatch, tmp_path):
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "p")
    monkeypatch.setenv("FLEETKIT_SECRET_ROOTS", f"{tmp_path / 'x'}:{tmp_path / 'y'}")
    s = Settings.from_env(flake=str(estate.repo))
    assert render.secret_roots(s, {"secretRoots": [str(tmp_path)]}) == [
        estate.repo, tmp_path, tmp_path / "x", tmp_path / "y"]


def test_a_declared_root_relative_to_the_checkout(estate):
    (estate.repo / "tenants" / "x" / "secrets").mkdir(parents=True)
    (estate.repo / "tenants" / "x" / SECRET).write_text("t")
    st = {"secretRoots": ["tenants/x"]}
    assert render.find_secret(estate.s, SECRET, st) == estate.repo / "tenants" / "x" / SECRET


@pytest.mark.parametrize("secrets", [(SECRET,), ()])
def test_a_declared_root_that_is_not_there_fails_by_name(estate, tmp_path, secrets):
    """Also for a stack that reads no file, and with the file elsewhere: what
    the flake declares has to be on this host. Nothing is planned or applied."""
    gone = "/nix/store/00000000000000000000000000000000-tenant-source"
    (estate.repo / "secrets").mkdir()
    (estate.repo / SECRET).write_text("own")
    w, _ = declare(estate, [root(tmp_path, "there", "t"), gone], secrets)
    with pytest.raises(render.RenderError) as e:
        deploy(estate)
    assert f"secret root {gone} (declared by the estate's flake: secretRoots) is not a directory" in str(e.value)
    assert w.calls == []
    # A file is not a root either, and a relative one is named as declared and as resolved.
    (tmp_path / "file").write_text("")
    with pytest.raises(render.RenderError, match=f"secret root {tmp_path / 'file'} .* is not a directory"):
        render.secret_roots(estate.s, {"secretRoots": [str(tmp_path / "file")]})
    with pytest.raises(render.RenderError, match=rf"secret root nope .* \({estate.repo}/nope\)"):
        render.secret_roots(estate.s, {"secretRoots": ["nope"]})


def test_a_backends_secret_is_found_under_a_declared_root(estate, tmp_path):
    """The connection string of a postgres backend, in the tenant's file."""
    tenant = root(tmp_path, "tenant", "postgres://db.example/state\n")
    sops = tmp_path / "sops"
    sops.write_text('#!/bin/sh\nfor a; do f=$a; done\ncat "$f"\n')  # "decrypts": prints the file
    sops.chmod(0o755)
    estate.s.sops = str(sops)
    _, st = declare(estate, [tenant], secrets=(),
                    backend={"type": "postgres", "urlSecret": {"path": SECRET, "extract": ["url"]}})
    env = backends.env_for(estate.s, "mini-guests", st["backend"], Emitter(lambda e: None), st)
    assert env["PULUMI_BACKEND_URL"] == "postgres://db.example/state"
    with pytest.raises(render.RenderError, match="is under none of"):
        backends.env_for(estate.s, "mini-guests", st["backend"], Emitter(lambda e: None), {**st, "secretRoots": []})
    # And by what resolves backends: a deploy, and an adoption (the stacks it
    # was asked for, and the estate's other stacks, which it reads too).
    other = fakes.program(pool2={"type": POOL, "properties": {"poolId": "two"}})
    other["name"] = "mini-other"
    estate.stack(other)
    estate.stacks["mini-other"].update(backend=st["backend"], secretRoots=st["secretRoots"])
    st["adoptIds"] = {"apps": "apps"}  # something to adopt: the other stack's state is read for the id
    estate.worlds["mini-guests"].live[(POOL, "apps")] = {"poolId": "apps"}
    report = adopt.run(estate.s, "mini", Emitter(lambda e: None), stacks=["mini-guests"])
    assert report["ok"] and "error" not in report["stacks"]["mini-guests"], report
    assert deploy(estate)["applied"] == ["mini-guests", "mini-other"]


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_a_gitops_job_uses_the_roots_of_its_own_commit(tmp_path, monkeypatch):
    """Two commits of the estate repo declare two roots (as two revisions of
    a tenant input are two store paths). Each job evaluates its checkout and
    reads its commit's root: nothing is kept from the job before, in either
    direction, and the server's own settings name no root."""
    a, b = root(tmp_path, "tenant-a", "a"), root(tmp_path, "tenant-b", "b")
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)

    def commit(roots):
        (origin / "flake.nix").write_text("{ outputs = _: { }; }\n")
        # What `nix eval <checkout>#pulumi` gives at this commit.
        (origin / "pulumi.json").write_text(json.dumps({"t": {
            "estate": "mini", "project": "t", "backend": None, "file": "/f", "secrets": [SECRET],
            "secretRoots": [str(r) for r in roots]}}))
        git("add", "-A", cwd=origin)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "c", cwd=origin)
        return git("rev-parse", "HEAD", cwd=origin)

    at_a, at_b, at_none = commit([a]), commit([b]), commit([])
    evals = []

    def nix_eval_json(s, attr, apply=None):
        assert attr == "pulumi" and apply == render.VIEW
        evals.append(s.flake.name)
        return json.loads((s.flake / "pulumi.json").read_text())
    monkeypatch.setattr(render, "nix_eval_json", nix_eval_json)

    def run(s2, req, ev):
        """The pipeline's render stage."""
        with render.Run(s2, "preview") as rundir:
            st = render.stacks_of(s2, req.estate)["t"]
            wd = rundir.dir / "t"
            wd.mkdir()
            render.link_secrets(s2, wd, st)
            return {"secret": os.readlink(wd / SECRET)}

    s = Settings(flake=None, state_dir=tmp_path / "state", passphrase="p", git=GitSettings(url=str(origin)))
    runner = make_runner(s, Repo(s), run=run)
    ev = Emitter(lambda e: None)

    def job(rev):
        return runner(DeployRequest(estate="mini", rev=rev, preview=True), ev)["secret"]

    assert [job(at_a), job(at_b), job(at_a)] == [str(a / SECRET), str(b / SECRET), str(a / SECRET)]
    with pytest.raises(render.RenderError, match="is under none of"):
        job(at_none)  # that commit declares none: not the roots of the jobs before it
    assert evals == [at_a, at_b, at_a, at_none]  # one evaluation per job, of its checkout
    assert s.extra_secret_roots == [] and s.flake is None


def test_the_real_engine_reads_a_file_under_a_declared_root(real, tmp_path):
    """Offline (conftest `real`): the program's output is the content of the
    file the render linked from the declared root."""
    tenant = root(tmp_path, "tenant", "from the tenant")
    real.stack({"name": "mini-guests", "runtime": "yaml",
                "resources": {"s": {"type": "random:index/randomString:RandomString", "properties": {"length": 4}}},
                "outputs": {"read": {"fn::readFile": f"./{SECRET}"}}})
    st = real.stacks["mini-guests"]
    st["secretRoots"], st["secrets"] = [str(tenant)], [SECRET]
    assert deploy(real)["applied"] == ["mini-guests"]
    env = {**os.environ, **backends.env_for(real.s, "mini-guests", st["backend"], Emitter(lambda e: None), st),
           "PULUMI_HOME": str(real.s.pulumi_home)}
    wd = tmp_path / "read"
    wd.mkdir()
    (wd / "Pulumi.yaml").write_text(json.dumps({"name": "mini-guests", "runtime": "yaml"}))
    out = subprocess.run(["pulumi", "stack", "output", "--json", "--stack", real.s.stack], cwd=wd, env=env,
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == {"read": "from the tenant"}
