"""fleet devtools — Developer tools and local services.

Groups secrets management and miscellaneous utilities.
"""
from __future__ import annotations

import click

from .secrets import secrets
from .sssd_test import sssd_test
from .utilities import utilities
from .reset_connection import reset_connection


@click.group("devtools")
def devtools():
    """Secrets and developer utilities.

    Groups SOPS secret management, connection-reset tooling, the
    directory-auth probe, and miscellaneous build helpers.
    """


devtools.add_command(secrets)
devtools.add_command(sssd_test)
devtools.add_command(utilities)
devtools.add_command(reset_connection)


@click.command("seed-inputs")
@click.argument("host")
@click.option("--ssh-key", default=None, metavar="PATH",
              help="Identity for the copy (default: whatever ssh config picks).")
@click.option("--user", default="root", show_default=True)
def seed_inputs(host: str, ssh_key: str | None, user: str) -> None:
    """Copy this flake's locked inputs into HOST's nix store.

    nix uses a locked input's store copy when the narHash matches and fetches
    only otherwise, so a CI runner seeded this way evaluates private inputs
    without a token of its own. HOST is a hosts.json name or an address. The
    inputs are fetched here, with your `gh auth` token when one is available.
    A stopgap to repeat after every `nix flake lock` bump — the durable path
    is an access token on the runner (fleetkit `infra.build.ciEnv.github`).
    """
    import os
    import shutil
    import subprocess
    import sys

    from ._util import find_project_root, fleet_cache_dir
    from .remote import _load_hosts

    root = find_project_root()
    addr = host
    if (fleet_cache_dir(root) / "hosts.json").exists():
        entry = _load_hosts().get(host)
        if entry:
            addr = entry.get("ip") or entry.get("internal_ip") or host
    env = dict(os.environ)
    if shutil.which("gh"):
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if tok.returncode == 0 and tok.stdout.strip():
            env["NIX_CONFIG"] = f"access-tokens = github.com={tok.stdout.strip()}"
    target = f"ssh://{user}@{addr}"
    if ssh_key:
        target += f"?ssh-key={ssh_key}"
    result = subprocess.run(["nix", "flake", "archive", "--to", target], cwd=root, env=env)
    sys.exit(result.returncode)


devtools.add_command(seed_inputs)
