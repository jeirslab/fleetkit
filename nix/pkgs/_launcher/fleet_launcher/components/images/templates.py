"""`deployer templates` — register built images as platform templates.

Each Proxmox verb reads the flake's `templates` reference object (ADR-0003) for
the stable identity to register under (file name, VMID, ostype). CLI flags
override it; by default the reference decides, so this tooling and fleetkit
agree on one contract even as the process underneath changes.
"""
from __future__ import annotations

import sys

import click

from . import nix
from .nix import DeployerError
from .platforms import docker as docker_platform
from .platforms import proxmox
from .platforms import xo


def _die(exc: Exception) -> None:
    click.secho(f"error: {exc}", fg="red", err=True)
    sys.exit(1)


def _ref(flake: str, key: str) -> dict:
    """The `templates.<key>` reference object.

    Reads the refs baked into the package ($FLEET_IMAGE_TEMPLATES — the
    eval-free artifact fleetkit ships, so this works from any consumer without
    a `templates` flake output), falling back to a runtime `nix eval` of the
    flake for dev/override.
    """
    import json
    import os
    from pathlib import Path

    baked = os.environ.get("FLEET_IMAGE_TEMPLATES")
    if baked and Path(baked).is_file():
        table = json.loads(Path(baked).read_text())
    else:
        table = nix.templates(nix.resolve_flake(flake))
    if key not in table:
        raise DeployerError(f"no template reference {key!r} (have: {', '.join(sorted(table))})")
    return table[key]


@click.group()
def templates() -> None:
    """Register bootstrap images as templates on their platform."""


@templates.group()
def register() -> None:
    """Register an image: proxmox-lxc | proxmox-vm | proxmox-vm-cloud | xen-orchestra | docker."""


_image = click.option("--image", required=True, type=click.Path(exists=True, dir_okay=False, resolve_path=True),
                      help="Artifact printed by `deployer images build`.")
_flake = click.option("--flake", default=".", show_default=True,
                      help="Flake holding the `templates` reference objects.")
_dry = click.option("--dry-run", is_flag=True, help="Show what would run; touch nothing.")


@register.command("proxmox-lxc")
@_image
@click.option("--host", required=True, help="PVE node (API + SSH).")
@click.option("--user", default="root", show_default=True, help="SSH user for the create commands.")
@click.option("--storage", default=None, help="vztmpl-capable storage (default: first the API advertises).")
@click.option("--name", default=None, help="Template name / variation (default: the reference's). Published as <name>-latest.tar.xz.")
@click.option("--version", "version", default=None, type=int, help="Pin a version: also publish <name>-v<N>.tar.xz alongside <name>-latest.")
@_flake
@_dry
def register_proxmox_lxc(image: str, host: str, user: str, storage: str | None, name: str | None,
                         version: int | None, flake: str, dry_run: bool) -> None:
    """Publish the CT template into <storage>:vztmpl/ as <name>-latest (+ optional -v<N>)."""
    try:
        ref = _ref(flake, "proxmox-lxc")
        proxmox.register_lxc(host=host, user=user, image=image, storage=storage, name=name,
                             version=version, ref=ref, dry_run=dry_run)
    except DeployerError as exc:
        _die(exc)


@register.command("proxmox-vm")
@_image
@click.option("--host", required=True, help="PVE node (API + SSH).")
@click.option("--user", default="root", show_default=True)
@click.option("--vmid", default=None, type=int, help="Template VMID (default: the reference object's).")
@click.option("--storage", default="local-lvm", show_default=True, help="Storage for the restored disk.")
@click.option("--name", default=None, help="Template name / variation (default: the reference's). Labelled <name>-latest.")
@click.option("--version", "version", default=None, type=int, help="Pin a version: the template is labelled <name>-v<N>.")
@click.option("--replace", is_flag=True, help="Destroy an existing VMID first (destructive).")
@_flake
@_dry
def register_proxmox_vm(image: str, host: str, user: str, vmid: int | None, storage: str, name: str | None,
                        version: int | None, replace: bool, flake: str, dry_run: bool) -> None:
    """qmrestore the vzdump archive into the template VMID and convert to a template."""
    try:
        ref = _ref(flake, "proxmox-vm")
        proxmox.register_vm(host=host, user=user, image=image, vmid=vmid, storage=storage, name=name,
                            version=version, replace=replace, ref=ref, dry_run=dry_run)
    except DeployerError as exc:
        _die(exc)


@register.command("proxmox-vm-cloud")
@_image
@click.option("--host", required=True, help="PVE node (API + SSH).")
@click.option("--user", default="root", show_default=True)
@click.option("--vmid", default=None, type=int, help="Template VMID (default: the reference object's).")
@click.option("--storage", default="local-lvm", show_default=True, help="Storage for the imported disk + cloud-init drive.")
@click.option("--name", default=None, help="Template name / variation (default: the reference's). Labelled <name>-latest.")
@click.option("--version", "version", default=None, type=int, help="Pin a version: the template is labelled <name>-v<N>.")
@click.option("--bridge", default="vmbr0", show_default=True, help="Bridge for net0.")
@click.option("--replace", is_flag=True, help="Destroy an existing VMID first (destructive).")
@_flake
@_dry
def register_proxmox_vm_cloud(image: str, host: str, user: str, vmid: int | None, storage: str, name: str | None,
                              version: int | None, bridge: str, replace: bool, flake: str, dry_run: bool) -> None:
    """Import the raw disk, add a cloud-init drive, convert to a template.

    Best-effort — validate against a live node (the deploy-side test is next).
    """
    try:
        ref = _ref(flake, "proxmox-vm-cloud")
        proxmox.register_vm_cloud(host=host, user=user, image=image, vmid=vmid, storage=storage, name=name,
                                  version=version, bridge=bridge, replace=replace, ref=ref, dry_run=dry_run)
    except DeployerError as exc:
        _die(exc)


@register.command("xen-orchestra")
@_image
@click.option("--name", required=True, help="Template name_label (versioned, e.g. nixos-bootstrap-v1).")
@click.option("--sr", required=True, help="SR name_label the root VDI lands on.")
@click.option("--network", default=None, help="Network name_label for the template's VIF (optional).")
@click.option("--firmware", type=click.Choice(["uefi", "bios"]), default="uefi", show_default=True)
@_dry
def register_xen_orchestra(image: str, name: str, sr: str, network: str | None, firmware: str, dry_run: bool) -> None:
    """Import the raw disk as a VDI, wrap it in a VM, convert to template (XO_URL/XO_TOKEN)."""
    try:
        xo.register(image=image, name=name, sr=sr, network=network, firmware=firmware, dry_run=dry_run)
    except DeployerError as exc:
        _die(exc)


@register.command("docker")
@_image
@click.option("--tag", required=True, help="Image tag, registry-qualified to push (ghcr.io/org/nixos-bootstrap:v1).")
@click.option("--push", is_flag=True, help="docker push after import.")
@_dry
def register_docker(image: str, tag: str, push: bool, dry_run: bool) -> None:
    """docker import the system tarball with /init as entrypoint; optionally push."""
    try:
        docker_platform.register(image=image, tag=tag, push=push, dry_run=dry_run)
    except DeployerError as exc:
        _die(exc)
