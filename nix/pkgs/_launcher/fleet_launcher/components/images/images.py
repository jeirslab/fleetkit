"""`deployer images` — build the golden bootstrap images."""
from __future__ import annotations

import platform
import sys

import click

from . import nix

DEFAULT_SYSTEM = {"x86_64": "x86_64-linux", "aarch64": "aarch64-linux", "arm64": "aarch64-linux"}.get(
    platform.machine(), "x86_64-linux")


def _die(exc: Exception) -> None:
    click.secho(f"error: {exc}", fg="red", err=True)
    sys.exit(1)


@click.group()
def images() -> None:
    """Build bootstrap images (one golden template, one format per platform)."""


@images.command("list")
@click.option("--flake", default=".", show_default=True, help="Flake reference holding the target table.")
def list_targets(flake: str) -> None:
    """Show the image targets, their formats and the artifact each produces."""
    try:
        url = nix.resolve_flake(flake)
        table = nix.targets(url)
    except nix.DeployerError as exc:
        _die(exc)
        return
    width = max(len(k) for k in table)
    for name, t in sorted(table.items()):
        click.echo(f"{name.ljust(width)}  format={t['format']:<12} artifact={t['artifact']:<20} {t['description']}")


@images.command("build")
@click.option("--target", "-t", required=True,
              help="Image target (see `deployer images list`): proxmox-lxc, proxmox-vm, xen-orchestra, docker.")
@click.option("--deploy-key", "deploy_key_file", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="OpenSSH PUBLIC key file baked into the image as root's authorized key.")
@click.option("--extra-key", "extra_key_files", multiple=True,
              type=click.Path(exists=True, dir_okay=False), help="Additional public key files (repeatable).")
@click.option("--host-name", default=None, help="Host name baked into the template (platforms may override).")
@click.option("--substituter", "substituters", multiple=True, help="Binary cache URL trusted from first boot (repeatable).")
@click.option("--trusted-public-key", "trusted_public_keys", multiple=True, help="Signing key for --substituter (repeatable, same order).")
@click.option("--system", default=DEFAULT_SYSTEM, show_default=True, help="Guest system (x86_64-linux, aarch64-linux).")
@click.option("--flake", default=".", show_default=True, help="Flake reference of fleetkit-deployer.")
@click.option("--out-link", "-o", default="result", show_default=True, help="Result symlink ('' for none).")
@click.option("--dry-run", is_flag=True, help="Print the nix command instead of running it.")
def build(target: str, deploy_key_file: str, extra_key_files: tuple[str, ...], host_name: str | None,
          substituters: tuple[str, ...], trusted_public_keys: tuple[str, ...], system: str, flake: str,
          out_link: str, dry_run: bool) -> None:
    """Build one image target with the given deploy key.

    Runs `nix build --impure --expr` against the flake's lib.mkBootstrapImage;
    the key content is passed as a Nix argument and is never written to disk.
    """
    try:
        key = nix.read_public_key(deploy_key_file)
        extra = [nix.read_public_key(p) for p in extra_key_files]
        url = nix.resolve_flake(flake)
        table = nix.targets(url)
        if target not in table:
            raise nix.DeployerError(f"unknown target {target!r}; known: {', '.join(sorted(table))}")
        click.echo(f"building {target} ({table[target]['format']}) for {system} from {url}", err=True)
        result = nix.build_image(
            url, target, key, system=system, out_link=out_link or None, dry_run=dry_run,
            extra_keys=extra, host_name=host_name,
            substituters=list(substituters), trusted_public_keys=list(trusted_public_keys),
        )
        if result is None:
            return
        artifact = nix.find_artifact(result, table[target]["artifact"])
    except nix.DeployerError as exc:
        _die(exc)
        return
    click.echo(f"result:   {result}", err=True)
    click.echo(f"artifact: {artifact}", err=True)
    click.echo(f"register: deployer templates register {table[target]['register']} --image {artifact} …", err=True)
    # stdout carries only the artifact path so the command composes in scripts.
    click.echo(artifact)
