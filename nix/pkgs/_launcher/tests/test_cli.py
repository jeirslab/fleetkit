"""Unit + integration tests for the fleet CLI composition and introspection.

Pure Python (no env/SOPS/network) so they run in the Nix sandbox via
pytestCheckHook. They cover the CLI-composition mechanism (register_cli_manifest
— the shared COMMANDS/ATTACH path used by consumer extensions and, from M6, the
component families) and the --dump-verbs surface introspection the
cli-verbs-golden gate depends on.
"""
from __future__ import annotations

import json

import click
from click.testing import CliRunner

from fleet_launcher.config import register_cli_manifest
from fleet_launcher.main import _describe_and_exit, _dump_verbs_and_exit, fleet


def _module(commands=None, attach=None):
    """A stand-in for an extension/family module exposing COMMANDS/ATTACH."""
    return type("M", (), {"COMMANDS": commands or [], "ATTACH": attach or {}})


# ── register_cli_manifest (unit) ──────────────────────────────────────

def test_commands_are_added_to_root():
    root = click.Group("root")

    @click.group("images")
    def images():  # pragma: no cover - body never invoked
        pass

    register_cli_manifest(root, _module(commands=[images]))
    assert "images" in root.commands


def test_attach_targets_an_existing_group():
    root = click.Group("root")

    @click.group("host")
    def host():  # pragma: no cover
        pass

    root.add_command(host)

    @click.command("nfs-sr")
    def nfs_sr():  # pragma: no cover
        pass

    register_cli_manifest(root, _module(attach={"host": [nfs_sr]}))
    assert "nfs-sr" in host.commands


def test_attach_to_unknown_group_warns_but_does_not_crash():
    root = click.Group("root")

    @click.command("orphan")
    def orphan():  # pragma: no cover
        pass

    # An ATTACH naming a group that does not exist must not raise, and must
    # not silently land the command on the root instead.
    register_cli_manifest(root, _module(attach={"nope": [orphan]}), source="test")
    assert "orphan" not in root.commands


# ── --dump-verbs introspection (integration) ─────────────────────────

def test_dump_verbs_emits_sorted_json(capsys):
    _dump_verbs_and_exit()
    verbs = json.loads(capsys.readouterr().out)
    assert isinstance(verbs, list) and verbs
    assert verbs == sorted(verbs), "verb paths must be sorted for a stable golden"
    # nested paths are space-joined
    assert "deploy" in verbs
    assert "deploy nixos" in verbs
    assert "inventory" in verbs


def test_dump_verbs_matches_walking_the_group(capsys):
    """The dump is exactly the reachable command tree — nothing dropped/added."""
    _dump_verbs_and_exit()
    dumped = set(json.loads(capsys.readouterr().out))

    reachable: set[str] = set()

    def walk(group: click.Group, prefix: str) -> None:
        ctx = click.Context(group)
        for name in group.list_commands(ctx):
            sub = group.get_command(ctx, name)
            path = f"{prefix}{name}"
            reachable.add(path)
            if isinstance(sub, click.Group):
                walk(sub, f"{path} ")

    walk(fleet, "")
    assert dumped == reachable


# ── fleet describe: the agent introspection surface (unit) ───────────

def test_describe_emits_command_tree_with_help_and_params(capsys, monkeypatch):
    # No baked env → commands only (options/components come from the wrapper).
    monkeypatch.delenv("FLEET_OPTIONS_JSON", raising=False)
    monkeypatch.delenv("FLEET_COMPONENTS_DIR", raising=False)
    _describe_and_exit()
    m = json.loads(capsys.readouterr().out)

    assert m["fleet"]["path"] == "fleet"
    paths = {c["path"] for c in m["commands"]}
    assert "describe" in paths, "describe must list itself"
    assert "templates register proxmox-lxc" in paths, "nested verbs are walked"
    # A known command carries its params (the deploy-key note etc.).
    lxc = next(c for c in m["commands"] if c["path"] == "templates register proxmox-lxc")
    assert any(p["name"] == "version" for p in lxc.get("params", []))


def test_describe_folds_in_options_and_components(capsys, monkeypatch, tmp_path):
    opts = tmp_path / "options.json"
    opts.write_text('{"fleet.settings.domain.internal": {"type": "str"}}')
    comp = tmp_path / "schema"
    (comp / "modules").mkdir(parents=True)
    (comp / "modules" / "infra.network.dns.json").write_text('{"options": {}}')
    (comp / "images").mkdir()
    (comp / "images" / "interface.json").write_text('{"proxmox-lxc": {}}')
    monkeypatch.setenv("FLEET_OPTIONS_JSON", str(opts))
    monkeypatch.setenv("FLEET_COMPONENTS_DIR", str(comp))
    _describe_and_exit()
    m = json.loads(capsys.readouterr().out)

    assert "fleet.settings.domain.internal" in m["options"]
    assert "infra.network.dns" in m["components"]["modules"]
    assert "proxmox-lxc" in m["components"]["images"]


# ── fleet CLI smoke (integration) ────────────────────────────────────

def test_fleet_help_runs_and_lists_groups():
    result = CliRunner().invoke(fleet, ["--help"])
    assert result.exit_code == 0, result.output
    for group in ("deploy", "inventory", "devtools"):
        assert group in result.output
