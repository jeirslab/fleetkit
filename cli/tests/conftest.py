"""An estate with one stack, rendered without nix and run by the fake engine
(fakes.py): what test_guard.py and test_adopt.py deploy and adopt. The same
estate with the real engine (`real`): test_real_pulumi.py."""
import json
import os
import shutil
import subprocess

import pytest

import fakes
from fleetkit_cli import infra, render
from fleetkit_cli.settings import Settings


class Estate:
    """`mini`: stacks whose programs the test sets, a fake engine world each."""

    def __init__(self, tmp_path, monkeypatch, fake=True):
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        (self.repo / "flake.nix").write_text("{ outputs = _: { }; }\n")
        self.s = Settings(flake=self.repo, state_dir=tmp_path / "state", passphrase="passphrase")
        self.stacks = {}
        self.worlds = {}
        monkeypatch.setattr(render, "stacks", lambda s: self.stacks)
        # nix build --out-link, without nix: the same link.
        monkeypatch.setattr(render, "_root", lambda s, link, target: (
            link.unlink() if link.is_symlink() or link.exists() else None, os.symlink(target, link)))
        if fake:
            monkeypatch.setattr(infra, "_stack", lambda s, wd, env: self.world_of(wd).stack(wd))

    def world_of(self, wd):
        prog = json.loads((wd / "Pulumi.yaml").read_text())
        return self.worlds[prog["name"]]

    def stack(self, prog, name=None, adopt_ids=None, unresolved=None):
        name = name or prog["name"]
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        f = store / f"{name}-Pulumi.yaml"
        f.write_text(json.dumps(prog))
        self.stacks[name] = {"estate": "mini", "project": prog["name"], "backend": {"type": "local", "path": "st"},
                             "file": str(f), "secrets": [], "adoptIds": adopt_ids or {},
                             "adoptUnresolved": unresolved or {}}
        return self.worlds.setdefault(prog["name"], fakes.World())

    def runs(self):
        """The run directories that are left under the state dir."""
        root = self.s.state_dir / "runs"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def import_anywhere(self):
        """Every file under the state dir or the repo that mentions an import option."""
        return [str(p) for root in (self.s.state_dir, self.repo) for p in root.rglob("*")
                if p.is_file() and '"import"' in p.read_text(errors="replace")]


@pytest.fixture
def estate(tmp_path, monkeypatch):
    return Estate(tmp_path, monkeypatch)


def pytest_terminal_summary(terminalreporter):
    """A skipped test is never silent: one SKIPPED line each, with why."""
    for rep in terminalreporter.stats.get("skipped", []):
        why = rep.longrepr[2] if isinstance(rep.longrepr, tuple) else str(rep.longrepr)
        terminalreporter.write_line(f"SKIPPED {rep.nodeid}: {why}")


def _real_pulumi():
    """-> (the pulumi binary, None) or (None, why the real engine cannot run here)."""
    exe = shutil.which("pulumi")
    if not exe:
        return None, "no pulumi binary on PATH"
    bindir = os.path.dirname(os.path.realpath(exe))
    for plugin in ("pulumi-language-yaml", "pulumi-resource-random", "pulumi-resource-tls"):
        if not (os.path.exists(os.path.join(bindir, plugin)) or shutil.which(plugin)):
            return None, f"{plugin} is not installed beside pulumi or on PATH (and nothing may be downloaded)"
    try:
        subprocess.run([exe, "version"], check=True, capture_output=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return None, f"pulumi version failed: {e}"
    return exe, None


@pytest.fixture
def real(tmp_path, monkeypatch):
    """The estate, run by the real Pulumi engine, offline: a file backend
    under tmp_path, PULUMI_HOME there too, and the `random` and `tls`
    providers that ship beside the binary (no network, no credentials)."""
    exe, why = _real_pulumi()
    if not exe:
        pytest.skip(f"real Pulumi not available: {why}")
    bindir = os.path.dirname(os.path.realpath(exe))
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PULUMI_SKIP_UPDATE_CHECK", "1")
    monkeypatch.setenv("PULUMI_DISABLE_AUTOMATIC_PLUGIN_ACQUISITION", "true")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    e = Estate(tmp_path, monkeypatch, fake=False)
    e.s.pulumi_home = tmp_path / "pulumi-home"
    e.s.pulumi_home.mkdir()
    return e
