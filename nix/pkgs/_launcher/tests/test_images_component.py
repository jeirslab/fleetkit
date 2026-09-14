"""Tests for the folded images component (fleet images / fleet templates).

Carried over from the former fleetkit-deployer and adapted to the vendored
module paths, plus integration tests proving the groups resolve upward under
`fleet`. Pure/sandbox-safe: the proxmox/xo back-ends lazy-import their heavy
deps, and only the pure helpers + dry-run paths are exercised here.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from fleet_launcher.components.images import nix
from fleet_launcher.components.images.platforms import proxmox, xo
from fleet_launcher.main import fleet


def _write_key(text: str) -> str:
    d = tempfile.mkdtemp()
    p = Path(d) / "key.pub"
    p.write_text(text)
    return str(p)


# ── nix helpers (unit) ────────────────────────────────────────────────

def test_read_public_key_accepts_ed25519():
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample deploy@example.com"
    assert nix.read_public_key(_write_key(key + "\n")) == key


def test_read_public_key_rejects_private_key():
    with pytest.raises(nix.DeployerError):
        nix.read_public_key(_write_key(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"))


def test_build_expression_is_a_function_of_deploy_key():
    expr = nix.build_expression("git+file:///repo", "proxmox-lxc", "x86_64-linux")
    assert expr.startswith("{ deployKey }:")
    assert "mkBootstrapImage" in expr
    assert "ssh-" not in expr, "the key must never be inlined into the expression"


def test_find_artifact_no_match_is_an_error():
    with pytest.raises(nix.DeployerError):
        nix.find_artifact(tempfile.mkdtemp(), "*.vma.zst")


# ── proxmox / xo back-end script builders (unit) ─────────────────────

def test_lxc_handles_latest_and_versioned():
    # default: latest only; pinned: latest + versioned (latest always exists)
    assert proxmox.lxc_handles("nixos-bootstrap-lxc", ".tar.xz", None) == ["nixos-bootstrap-lxc-latest.tar.xz"]
    assert proxmox.lxc_handles("nixos-bootstrap-lxc", ".tar.xz", 3) == [
        "nixos-bootstrap-lxc-latest.tar.xz",
        "nixos-bootstrap-lxc-v3.tar.xz",
    ]


def test_lxc_install_publishes_each_handle_idempotently():
    s = proxmox._lxc_install(
        remote_tmp="/var/tmp/x", storage="local",
        names=["nixos-bootstrap-lxc-latest.tar.xz", "nixos-bootstrap-lxc-v3.tar.xz"], digest="ab" * 32)
    assert "local:vztmpl/nixos-bootstrap-lxc-latest.tar.xz" in s
    assert "local:vztmpl/nixos-bootstrap-lxc-v3.tar.xz" in s
    assert "up to date" in s  # checksum-idempotent per handle


def test_vm_label_latest_and_versioned():
    assert proxmox._vm_label("nixos-bootstrap-vm", None) == "nixos-bootstrap-vm-latest"
    assert proxmox._vm_label("nixos-bootstrap-vm", 2) == "nixos-bootstrap-vm-v2"


def test_vm_restore_guards_existing_vmid_without_replace():
    s = proxmox._vm_restore(remote_tmp="/var/tmp/x.vma.zst", vmid=9000, storage="local-lvm", name="n", replace=False)
    assert "use --replace" in s
    assert "qmrestore" in s


def test_xo_ws_url():
    assert xo._ws_url("https://xo.example.com") == "wss://xo.example.com/api/"


# ── resolve-upward + dry-run (integration) ───────────────────────────

def test_images_and_templates_resolve_under_fleet():
    r = CliRunner().invoke(fleet, ["images", "--help"])
    assert r.exit_code == 0, r.output
    r2 = CliRunner().invoke(fleet, ["templates", "register", "--help"])
    assert r2.exit_code == 0, r2.output
    assert "proxmox-lxc" in r2.output


def test_templates_register_docker_dry_run():
    d = tempfile.mkdtemp()
    tarball = Path(d) / "sys.tar.xz"
    tarball.write_bytes(b"")
    r = CliRunner().invoke(fleet, [
        "templates", "register", "docker",
        "--image", str(tarball), "--tag", "example/nixos-bootstrap:test", "--dry-run",
    ])
    assert r.exit_code == 0, r.output
    assert "docker import" in r.output
