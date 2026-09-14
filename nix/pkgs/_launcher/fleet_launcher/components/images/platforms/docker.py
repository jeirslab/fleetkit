"""Docker registration: system tarball → image (docker import) → optional push.

The docker target is a NixOS system tarball whose /init boots systemd, so
it is imported (not loaded) with /init as the entrypoint. Registry login is
the daemon's own (`docker login`); the CLI never handles registry secrets.
"""
from __future__ import annotations

import shlex
import subprocess

from ..nix import DeployerError


def _run(cmd: list[str], *, dry_run: bool) -> None:
    if dry_run:
        print(" ".join(shlex.quote(c) for c in cmd))
        return
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0:
        raise DeployerError(f"{' '.join(cmd[:2])} failed (exit {proc.returncode})")


def register(*, image: str, tag: str, push: bool, dry_run: bool) -> None:
    if not image.endswith((".tar.xz", ".tar.gz", ".tar")):
        raise DeployerError("expected the system tarball (build target docker)")
    if "/" not in tag and push:
        raise DeployerError("--push needs a registry-qualified tag (ghcr.io/<org>/<name>:<tag>)")
    _run(["docker", "import", "--change", 'ENTRYPOINT ["/init"]', image, tag], dry_run=dry_run)
    if push:
        _run(["docker", "push", tag], dry_run=dry_run)
    print(f"registered: {tag} — run with: docker run --privileged -d {tag}")
