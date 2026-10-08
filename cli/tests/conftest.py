"""An estate with one stack, rendered without nix and run by the fake engine
(fakes.py): what test_guard.py and test_adopt.py deploy and adopt."""
import json
import os

import pytest

import fakes
from fleetkit_cli import infra, render
from fleetkit_cli.settings import Settings


class Estate:
    """`mini`: stacks whose programs the test sets, a fake engine world each."""

    def __init__(self, tmp_path, monkeypatch):
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

    def workdir(self, name="mini-guests"):
        return self.s.state_dir / "work" / name

    def import_anywhere(self):
        """Every file under the state dir or the repo that mentions an import option."""
        return [str(p) for root in (self.s.state_dir, self.repo) for p in root.rglob("*")
                if p.is_file() and '"import"' in p.read_text(errors="replace")]


@pytest.fixture
def estate(tmp_path, monkeypatch):
    return Estate(tmp_path, monkeypatch)
