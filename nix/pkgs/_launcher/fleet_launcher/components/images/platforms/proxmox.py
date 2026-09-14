"""Proxmox VE registration: proxmoxer (API reads) + paramiko (upload + CLI).

The work is split by what each tool is best at:

* **proxmoxer** (HTTPS API, token auth) does the STRUCTURED reads — which
  storages advertise `vztmpl`, whether a VMID is taken, converting a VM to a
  template, and reading a config back to VERIFY the result. No output parsing.
* **paramiko** (SSH) does the two things the API cannot do cleanly for a local
  artifact: SFTP the file onto the node, and run PVE's own create commands
  (`pvesm`, `qmrestore`, `qm importdisk`, `qm`). The MUTATING steps stay on the
  battle-tested CLI; only the transport changed from shelling out to `ssh`.

The identity a template lands under — its file name, VMID, ostype — is NOT
decided here: it comes from the flake's `templates` reference object (ADR-0003),
passed in as `ref`, so this tooling and fleetkit agree on one contract.

Credentials come from the environment only:
    API : PVE_TOKEN_ID     = "user@realm!tokenname"
          PVE_TOKEN_SECRET = "<uuid>"
          PVE_VERIFY_SSL   = "1" (default) | "0"
    SSH : the ssh-agent / default keys of --user@--host (paramiko).
Node name defaults to the short host name; override with PVE_NODE.

proxmoxer and paramiko are imported lazily so `deployer --help` and the
non-Proxmox verbs never require them.
"""
from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path

from ..nix import DeployerError


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── connections ───────────────────────────────────────────────────────────

def _node_name(host: str) -> str:
    return os.environ.get("PVE_NODE") or host.split(".")[0]


def _api(host: str):
    """A proxmoxer ProxmoxAPI from PVE_TOKEN_ID / PVE_TOKEN_SECRET."""
    try:
        from proxmoxer import ProxmoxAPI
    except ImportError as exc:  # pragma: no cover
        raise DeployerError("proxmoxer is required for Proxmox registration (pip/uv: proxmoxer)") from exc
    token_id = os.environ.get("PVE_TOKEN_ID")
    secret = os.environ.get("PVE_TOKEN_SECRET")
    if not (token_id and secret):
        raise DeployerError("set PVE_TOKEN_ID ('user@realm!tokenname') and PVE_TOKEN_SECRET for the PVE API")
    if "!" not in token_id:
        raise DeployerError("PVE_TOKEN_ID must be 'user@realm!tokenname'")
    user, token_name = token_id.split("!", 1)
    verify = os.environ.get("PVE_VERIFY_SSL", "1").lower() not in ("0", "false", "no", "")
    try:
        return ProxmoxAPI(host, user=user, token_name=token_name, token_value=secret, verify_ssl=verify)
    except Exception as exc:  # proxmoxer wraps auth/transport errors broadly
        raise DeployerError(f"cannot reach the PVE API at {host}: {exc}") from exc


def _ssh(host: str, user: str):
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover
        raise DeployerError("paramiko is required for Proxmox registration (pip/uv: paramiko)") from exc
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, username=user, allow_agent=True, look_for_keys=True, timeout=15)
    except Exception as exc:
        raise DeployerError(f"ssh {user}@{host} failed: {exc}") from exc
    return client


def _exec(ssh, script: str) -> str:
    """Run a bash script on the node; raise with stderr on non-zero exit."""
    _in, out, err = ssh.exec_command(f"bash -eo pipefail -c {shlex.quote(script)}")
    stdout = out.read().decode()
    stderr = err.read().decode()
    rc = out.channel.recv_exit_status()
    if rc != 0:
        raise DeployerError(f"remote command failed (exit {rc}):\n{(stderr or stdout).strip()}")
    return stdout


def _put(ssh, local: str, remote: str) -> None:
    sftp = ssh.open_sftp()
    try:
        sftp.put(local, remote)
    finally:
        sftp.close()


# ── LXC: upload the tarball into <storage>:vztmpl/<file> ────────────────────

def _lxc_install(*, remote_tmp: str, storage: str, name: str, digest: str) -> str:
    volid = f"{storage}:vztmpl/{name}"
    return f"""
DEST="$(pvesm path {shlex.quote(volid)})"
if [ -f "$DEST" ] && [ "$(sha256sum "$DEST" | cut -d' ' -f1)" = "{digest}" ]; then
  echo "up to date: {volid}"; rm -f {shlex.quote(remote_tmp)}; exit 0
fi
[ "$(sha256sum {shlex.quote(remote_tmp)} | cut -d' ' -f1)" = "{digest}" ] \
  || {{ echo "upload checksum mismatch" >&2; exit 1; }}
mkdir -p "$(dirname "$DEST")"
install -m0644 {shlex.quote(remote_tmp)} "$DEST.tmp.$$" && mv -f "$DEST.tmp.$$" "$DEST"
rm -f {shlex.quote(remote_tmp)}
"""


def register_lxc(*, host: str, user: str, image: str, storage: str | None, name: str | None,
                 ref: dict, dry_run: bool) -> None:
    name = name or ref["file"]  # stable handle from the reference object (ADR-0003)
    if not name.endswith(".tar.xz"):
        raise DeployerError("LXC template name must end in .tar.xz (pct requires it)")
    digest = sha256(image)
    node = _node_name(host)

    if dry_run:
        print(f"# upload {image} → {host} ({node}) as {storage or '<first vztmpl storage>'}:vztmpl/{name}")
        print(f"#   sha256 {digest}; ostype={ref['ostype']} unprivileged={int(ref['unprivileged'])}")
        return

    api = _api(host)
    stores = [s["storage"] for s in api.nodes(node).storage.get(content="vztmpl")]
    if not stores:
        raise DeployerError(f"{node}: no storage advertises vztmpl content")
    if storage and storage not in stores:
        raise DeployerError(f"{node}: storage {storage!r} has no vztmpl content (have: {', '.join(stores)})")
    storage = storage or stores[0]
    volid = f"{storage}:vztmpl/{name}"

    ssh = _ssh(host, user)
    try:
        remote_tmp = f"/var/tmp/{name}.upload"
        _put(ssh, image, remote_tmp)
        _exec(ssh, _lxc_install(remote_tmp=remote_tmp, storage=storage, name=name, digest=digest))
    finally:
        ssh.close()

    present = [c["volid"] for c in api.nodes(node).storage(storage).content.get(content="vztmpl")]
    if volid not in present:
        raise DeployerError(f"registration ran but {volid} is not listed on {storage}")
    print(f"registered: {volid}")
    print(f"clone with: pct create <vmid> {volid} --ostype {ref['ostype']} --unprivileged {int(ref['unprivileged'])} …")


# ── VM (VMA): qmrestore the vzdump into a VMID, convert to a template ───────

def _vm_restore(*, remote_tmp: str, vmid: int, storage: str, name: str, replace: bool) -> str:
    guard = f"qm destroy {vmid} --purge" if replace else 'echo "VMID exists; use --replace" >&2; exit 1'
    return f"""
if qm status {vmid} >/dev/null 2>&1; then
  {guard}
fi
qmrestore {shlex.quote(remote_tmp)} {vmid} --storage {shlex.quote(storage)} --unique
qm set {vmid} --name {shlex.quote(name)} --agent enabled=1
rm -f {shlex.quote(remote_tmp)}
"""


def register_vm(*, host: str, user: str, image: str, vmid: int | None, storage: str, name: str | None,
                replace: bool, ref: dict, dry_run: bool) -> None:
    vmid = vmid if vmid is not None else ref["vmid"]
    name = name or ref["name"]
    if not image.endswith((".vma", ".vma.zst", ".vma.gz", ".vma.lzo")):
        raise DeployerError("expected a vzdump archive (.vma[.zst]) — build target proxmox-vm")
    node = _node_name(host)

    if dry_run:
        print(f"# qmrestore {image} → {host} ({node}) VMID {vmid} on {storage}, name={name}, then template")
        return

    api = _api(host)
    if any(int(v["vmid"]) == int(vmid) for v in api.nodes(node).qemu.get()) and not replace:
        raise DeployerError(f"VMID {vmid} exists on {node}; pass --replace or pick another --vmid")

    ssh = _ssh(host, user)
    try:
        remote_tmp = f"/var/tmp/{os.path.basename(image)}"
        _put(ssh, image, remote_tmp)
        _exec(ssh, _vm_restore(remote_tmp=remote_tmp, vmid=vmid, storage=storage, name=name, replace=replace))
    finally:
        ssh.close()

    api.nodes(node).qemu(vmid).template.post()
    if not api.nodes(node).qemu(vmid).config.get().get("template"):
        raise DeployerError(f"VMID {vmid} did not convert to a template")
    print(f"registered: template VMID {vmid} ({name}) — clone with: qm clone {vmid} <newid> --full")


# ── VM (cloud image): import the raw disk, add a cloud-init drive, template ─
#
# BEST-EFFORT: the importdisk → attach → cloud-init flow is assembled here from
# the standard PVE recipe but has NOT been validated against a live node yet —
# the deploy-side test is the next phase. Kept explicit so the shape is
# reviewable; expect to adjust volid parsing / storage assumptions on first run.

def _vm_cloud_build(*, remote_tmp: str, vmid: int, storage: str, name: str, bridge: str, replace: bool) -> str:
    guard = f"qm destroy {vmid} --purge" if replace else 'echo "VMID exists; use --replace" >&2; exit 1'
    return f"""
if qm status {vmid} >/dev/null 2>&1; then
  {guard}
fi
qm create {vmid} --name {shlex.quote(name)} --memory 2048 --cores 2 --cpu host \
  --net0 virtio,bridge={shlex.quote(bridge)} --serial0 socket --vga serial0 \
  --ostype l26 --scsihw virtio-scsi-single --agent enabled=1
qm importdisk {vmid} {shlex.quote(remote_tmp)} {shlex.quote(storage)}
UNUSED="$(qm config {vmid} | awk -F': ' '/^unused[0-9]+:/ {{print $2; exit}}')"
[ -n "$UNUSED" ] || {{ echo "importdisk produced no unused disk" >&2; exit 1; }}
qm set {vmid} --scsi0 "$UNUSED"
qm set {vmid} --ide2 {shlex.quote(storage)}:cloudinit
qm set {vmid} --boot order=scsi0
rm -f {shlex.quote(remote_tmp)}
"""


def register_vm_cloud(*, host: str, user: str, image: str, vmid: int | None, storage: str, name: str | None,
                      bridge: str, replace: bool, ref: dict, dry_run: bool) -> None:
    vmid = vmid if vmid is not None else ref["vmid"]
    name = name or ref["name"]
    if not image.endswith(".img"):
        raise DeployerError("expected a raw disk (.img) — build target proxmox-vm-cloud")
    node = _node_name(host)

    if dry_run:
        print(f"# import {image} → {host} ({node}) VMID {vmid} on {storage}, cloud-init drive, then template")
        return

    api = _api(host)
    if any(int(v["vmid"]) == int(vmid) for v in api.nodes(node).qemu.get()) and not replace:
        raise DeployerError(f"VMID {vmid} exists on {node}; pass --replace or pick another --vmid")

    ssh = _ssh(host, user)
    try:
        remote_tmp = f"/var/tmp/{os.path.basename(image)}"
        _put(ssh, image, remote_tmp)
        _exec(ssh, _vm_cloud_build(remote_tmp=remote_tmp, vmid=vmid, storage=storage, name=name,
                                   bridge=bridge, replace=replace))
    finally:
        ssh.close()

    api.nodes(node).qemu(vmid).template.post()
    if not api.nodes(node).qemu(vmid).config.get().get("template"):
        raise DeployerError(f"VMID {vmid} did not convert to a template")
    print(f"registered: cloud-init template VMID {vmid} ({name}) — clone, then set --ciuser/--ipconfig0 per host")
