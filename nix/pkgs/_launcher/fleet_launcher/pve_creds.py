"""Per-provider-instance Proxmox VE credentials from SOPS.

A multi-site fleet has one PVE endpoint per site, each with its own API
token (`fleet.providers.proxmox.<instance>` declares the endpoint and the
sops path of the token). The launcher's startup export
(main._setup_env) fills PROXMOX_VE_* from the FLAT `integrations.proxmox`
keys, or from the first instance in sorted order when there is no flat
shape — one site's credentials, whichever the sort picks. Anything that
talks to a specific instance's API (the adopter's live check, `fleet pve
--instance`) resolves that instance's credentials here instead.

Layout read, under `integrations.proxmox.<instance>` in the integrations
SOPS file: `endpoint`, `api_token` (user@realm!tokenid=uuid, preferred),
`username` (or the legacy `api_user`) + `password`, `insecure`
(default true — every fleet PVE endpoint is on a self-signed cert until
its step-ca issues for it).
"""
from __future__ import annotations

import subprocess


def instance_credentials(instance: str) -> dict:
    """Return {endpoint, api_token, username, password, insecure} for one
    provider instance, or raise with the reason (never exit the process:
    callers degrade to "unverified", not to a dead dry run)."""
    from .main import _find_sops
    from .config import integrations_file

    sops = _find_sops()
    if not sops:
        raise RuntimeError("sops not found on PATH — cannot read instance credentials")
    result = subprocess.run(
        [sops, "-d", "--extract", f'["integrations"]["proxmox"]["{instance}"]',
         str(integrations_file())],
        capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(
            f"no credentials for proxmox instance {instance!r} under "
            f"integrations.proxmox.{instance} — {result.stderr.strip()[:160]}")
    import yaml
    data = yaml.safe_load(result.stdout) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"integrations.proxmox.{instance} is not a mapping")
    return {
        "endpoint": str(data.get("endpoint") or ""),
        "api_token": str(data.get("api_token") or ""),
        "username": str(data.get("username") or data.get("api_user") or "root@pam"),
        "password": str(data.get("password") or ""),
        "insecure": str(data.get("insecure", "true")).lower() in ("true", "1", "yes"),
    }


def instance_of_provider_ref(ref: str | None) -> str | None:
    """`proxmox.dell-2` (a resource's `provider` field in config.tf.json)
    → `dell-2`; None when the field is absent or not a proxmox alias."""
    if not ref or not isinstance(ref, str):
        return None
    head, _, inst = ref.partition(".")
    return inst or None if head == "proxmox" else None
