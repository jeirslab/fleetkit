"""Driving `nix` for image builds.

The images are deliberately not flake packages (a keyless golden image is a
foot-gun), so the CLI evaluates `lib.mkBootstrapImage` with the key passed as
an argument:

    nix build --impure --expr '{ deployKey, ... }: (builtins.getFlake FLAKE).lib.mkBootstrapImage {…}' \
        --argstr deployKey "ssh-ed25519 …"

`--impure` is needed only because `builtins.getFlake` on an unlocked
(dirty-tree) reference is impure; the image evaluation itself reads nothing
from the environment. The flake reference is resolved through
`nix flake metadata` so ".", a path, or `github:…` all work.
"""
from __future__ import annotations

import glob
import json
import os
import shlex
import subprocess
from pathlib import Path

PUBLIC_KEY_PREFIXES = ("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-", "sk-ssh-ed25519@", "sk-ecdsa-sha2-")


class DeployerError(RuntimeError):
    """A failure the operator can act on; printed without a traceback."""


def read_public_key(path: str) -> str:
    """Read an OpenSSH public key file, refusing private keys early.

    The Nix module checks again at evaluation time; this check exists so the
    error arrives before a nixpkgs evaluation, with the file name in it.
    """
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DeployerError(f"cannot read deploy key {path}: {exc}") from exc
    if "PRIVATE KEY" in text:
        raise DeployerError(f"{path} is a PRIVATE key — pass the public half (.pub)")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if len(lines) != 1 or not lines[0].startswith(PUBLIC_KEY_PREFIXES):
        raise DeployerError(f"{path} does not contain exactly one OpenSSH public key")
    return lines[0]


def resolve_flake(ref: str) -> str:
    """Return the URL nix itself resolves `ref` to (git+file://…, github:…)."""
    try:
        out = subprocess.run(
            ["nix", "flake", "metadata", "--json", ref],
            check=True, capture_output=True, text=True,
        ).stdout
    except FileNotFoundError as exc:
        raise DeployerError("nix not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise DeployerError(f"nix flake metadata {ref} failed:\n{exc.stderr.strip()}") from exc
    url = json.loads(out).get("url")
    if not url:
        raise DeployerError(f"nix flake metadata {ref}: no url in output")
    return url


def targets(flake_url: str) -> dict[str, dict]:
    """The flake's `targets` output (plain data, see lib/default.nix)."""
    try:
        out = subprocess.run(
            ["nix", "eval", "--json", f"{flake_url}#targets"],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise DeployerError(f"nix eval {flake_url}#targets failed:\n{exc.stderr.strip()}") from exc
    return json.loads(out)


def templates(flake_url: str) -> dict[str, dict]:
    """The flake's `templates` output — the stable reference objects (ADR-0003).

    Both this CLI (to know the name/VMID/ostype to create) and fleetkit (to know
    what to clone) read this one object, so the registration process can change
    without fleetkit changing.
    """
    try:
        out = subprocess.run(
            ["nix", "eval", "--json", f"{flake_url}#templates"],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise DeployerError(f"nix eval {flake_url}#templates failed:\n{exc.stderr.strip()}") from exc
    return json.loads(out)


def build_expression(flake_url: str, target: str, system: str, *,
                     extra_keys: list[str] | None = None,
                     host_name: str | None = None,
                     substituters: list[str] | None = None,
                     trusted_public_keys: list[str] | None = None) -> str:
    """The Nix expression `nix build --expr` evaluates (a function of deployKey)."""
    args = {
        "system": system,
        "target": target,
        "extraAuthorizedKeys": extra_keys or [],
        "substituters": substituters or [],
        "trustedPublicKeys": trusted_public_keys or [],
    }
    if host_name:
        args["hostName"] = host_name
    # JSON is a subset of Nix for strings, lists and objects of those.
    nix_args = json.dumps(args)
    return (
        "{ deployKey }: "
        f'(builtins.getFlake {json.dumps(flake_url)}).lib.mkBootstrapImage '
        f"(builtins.fromJSON {json.dumps(nix_args)} // {{ inherit deployKey; }})"
    )


def build_image(flake_url: str, target: str, deploy_key: str, *, system: str,
                out_link: str | None, dry_run: bool = False, **kwargs) -> str | None:
    """Run the build; return the result path (None on --dry-run)."""
    expr = build_expression(flake_url, target, system, **kwargs)
    cmd = ["nix", "build", "--impure", "--expr", expr, "--argstr", "deployKey", deploy_key,
           "--print-out-paths"]
    cmd += ["--out-link", out_link] if out_link else ["--no-link"]
    if dry_run:
        print(" ".join(shlex.quote(c) for c in cmd))
        return None
    proc = subprocess.run(cmd, text=True, capture_output=False, stdout=subprocess.PIPE)
    if proc.returncode != 0:
        raise DeployerError(f"nix build failed for target {target} (exit {proc.returncode})")
    paths = [p for p in proc.stdout.split() if p]
    if not paths:
        raise DeployerError("nix build printed no output path")
    return paths[-1]


def find_artifact(result: str, pattern: str) -> str:
    """The single file `pattern` (relative glob) matches under the result."""
    matches = sorted(glob.glob(os.path.join(result, pattern)))
    if len(matches) != 1:
        listing = "\n  ".join(sorted(str(p) for p in Path(result).rglob("*") if p.is_file())[:20])
        raise DeployerError(
            f"expected exactly one {pattern} under {result}, found {len(matches)}; files:\n  {listing}")
    return matches[0]
