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


def test_pve_api_params_prefers_fleetkit_env(monkeypatch):
    # fleetkit's canonical env wins, even if the deployer's PVE_* is also set
    monkeypatch.setenv("PROXMOX_VE_ENDPOINT", "https://10.1.1.2:8006")
    monkeypatch.setenv("PROXMOX_VE_API_TOKEN", "root@pam!fleet=uuid-secret")
    monkeypatch.setenv("PROXMOX_VE_INSECURE", "true")
    monkeypatch.setenv("PVE_TOKEN_ID", "should@pve!be-ignored")
    assert proxmox._pve_api_params() == {
        "host": "10.1.1.2:8006", "user": "root@pam", "token_name": "fleet",
        "token_value": "uuid-secret", "verify_ssl": False,
    }


def test_pve_api_params_falls_back_to_pve_env(monkeypatch):
    for var in ("PROXMOX_VE_ENDPOINT", "PROXMOX_VE_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PVE_TOKEN_ID", "deployer@pve!ci")
    monkeypatch.setenv("PVE_TOKEN_SECRET", "sekret")
    p = proxmox._pve_api_params()
    assert (p["user"], p["token_name"], p["token_value"]) == ("deployer@pve", "ci", "sekret")


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


def test_ref_reads_baked_templates_eval_free(monkeypatch, tmp_path):
    # $FLEET_IMAGE_TEMPLATES is read directly — no `nix eval`, no flake needed.
    from fleet_launcher.components.images.templates import _ref
    j = tmp_path / "image-templates.json"
    j.write_text('{"proxmox-lxc": {"name": "nixos-bootstrap-lxc", "ostype": "nixos"}}')
    monkeypatch.setenv("FLEET_IMAGE_TEMPLATES", str(j))
    assert _ref(".", "proxmox-lxc")["name"] == "nixos-bootstrap-lxc"


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
