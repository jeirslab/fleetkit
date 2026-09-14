"""fleet pve — Proxmox VE host management commands."""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

from . import config as cfg
from ._util import find_project_root
from .pve_api import get_host

console = Console()


def _nix_str_list(values: list[str]) -> str:
    """Render a Python list of strings as a Nix list literal.

    For `nix-build --arg`, which takes an expression rather than a string.
    `${` is escaped because Nix would otherwise read it as antiquotation —
    no cache URL or signing key contains one, but a silent interpolation is
    a worse failure than a redundant backslash.
    """
    def one(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${") + '"'

    return "[ " + " ".join(one(v) for v in values) + " ]"


def _get_pve_config(host_override: str | None = None) -> tuple[str, str]:
    """Return (pve_host, pve_user) from env or override."""
    pve_host = host_override or get_host()
    if not pve_host:
        console.print("[red]ERROR:[/red] PROXMOX_VE_ENDPOINT not set (source .env)")
        sys.exit(1)
    pve_user = "root"
    return pve_host, pve_user


def _ssh_cmd(host: str, user: str = "root") -> list[str]:
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{user}@{host}",
    ]


@click.group("pve")
def pve():
    """Proxmox VE host management."""
    pass


@pve.command("install-nix")
@click.option("--host", default=None, help="PVE host IP (defaults to PROXMOX_VE_ENDPOINT)")
def install_nix(host: str | None):
    """Install Determinate Nix on the Proxmox host."""
    pve_host, pve_user = _get_pve_config(host)

    console.print(f"Installing Nix on [bold]{pve_host}[/bold]...")

    # Check if nix is already installed
    check = subprocess.run(
        _ssh_cmd(pve_host, pve_user) + ["command -v nix"],
        capture_output=True, text=True,
    )
    if check.returncode == 0:
        console.print("[green]Nix already installed[/green]")
        return

    # Install Determinate Nix
    install_script = (
        'curl --proto "=https" --tlsv1.2 -sSf -L https://install.determinate.systems/nix'
        " | sh -s -- install linux --init none --no-confirm"
    )
    result = subprocess.run(
        _ssh_cmd(pve_host, pve_user) + [install_script],
        text=True,
    )
    if result.returncode != 0:
        console.print("[red]ERROR:[/red] Nix installation failed")
        sys.exit(1)

    # Wire into .bashrc
    bashrc_snippet = """
if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
  . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
fi
"""
    subprocess.run(
        _ssh_cmd(pve_host, pve_user) + [
            f'grep -q "nix-daemon.sh" /root/.bashrc 2>/dev/null || echo \'{bashrc_snippet}\' >> /root/.bashrc'
        ],
        text=True,
    )
    console.print("[green]Nix installation complete[/green]")


@pve.command("build-template")
@click.option("--host", default=None, help="PVE host IP (defaults to PROXMOX_VE_ENDPOINT)")
@click.option("--type", "image_type", type=click.Choice(["lxc", "vm"]), default="lxc",
              help="Image type: lxc (container template) or vm (QEMU VM image)")
@click.option("--builder-nix", default=None,
              help="Path to the NixOS image builder .nix file on the PVE host (auto-detected)")
@click.option("--template-name", default=None,
              help="Output filename (auto-detected from type)")
@click.option("--storage", default=None, help="Target PVE storage (auto-detect if omitted)")
def build_template(host: str | None, image_type: str, builder_nix: str | None, template_name: str | None, storage: str | None):
    """Build a NixOS template/image on the Proxmox host.

    Builds either an LXC container template (.tar.xz) or a VM image (.vma.zst)
    using nix-build on the PVE host, then installs it to Proxmox storage.

    Examples:
      fleet bootstraps pve build-template              # LXC (default)
      fleet bootstraps pve build-template --type vm    # VM image
    """
    pve_host, pve_user = _get_pve_config(host)

    # Ship the canonical image definition unless the operator pointed at their
    # own with --builder-nix. It used to have to be hand-copied to
    # /root/builder/ on the hypervisor, where it drifted from the repo
    # silently: the build kept succeeding against the stale copy, so a
    # template fix could land in git and never reach a single container. The
    # shipped file goes to its own path so an operator's file is left alone —
    # it just stops being what gets built.
    build_args: list[str] = []
    if builder_nix is None:
        from .ansible_group import framework_images_dir

        images = framework_images_dir()
        if images is None:
            console.print("[red]ERROR:[/red] cannot locate fleetkit's nix/images/ tree "
                          "(set FLEET_IMAGES_DIR or pass --builder-nix)")
            sys.exit(1)
        src = images / "by-platform" / "proxmox.nix"
        if not src.is_file():
            console.print(f"[red]ERROR:[/red] {src} not found")
            sys.exit(1)

        builder_nix = "/root/builder/fleetkit-proxmox.nix"
        console.print(f"Shipping [bold]{src}[/bold] → {pve_host}:{builder_nix}")
        subprocess.run(
            _ssh_cmd(pve_host, pve_user) + ["mkdir -p /root/builder"],
            check=True, text=True,
        )
        scp = subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
             "-o", "BatchMode=yes", str(src), f"{pve_user}@{pve_host}:{builder_nix}"],
            text=True,
        )
        if scp.returncode != 0:
            console.print("[red]ERROR:[/red] failed to ship the image definition")
            sys.exit(1)

        # The image's parameters come from the fleet manifest, not from a
        # value edited into the copy on the hypervisor. sshPubKey is what
        # Colmena's first push authenticates with; the cache pair is why a
        # fresh container substitutes its first closure instead of building
        # it on 2 vCPUs (see the nix.settings note in the image).
        build_args += ["--argstr", "sshPubKey", cfg.require(
            "network.sysadmin_ssh_key",
            "set fleet.network.sysadmin_ssh_key and rebuild the catalog",
        )]
        build_args += [
            "--arg", "substituters", _nix_str_list(cfg.get("cache.substituters", []) or []),
            "--arg", "trustedPublicKeys", _nix_str_list(cfg.get("cache.trusted_public_keys", []) or []),
            "--arg", "type", f'"{image_type}"',
        ]
    elif image_type == "vm":
        # An operator's own file is only assumed to take `type`, and only
        # where the old default filenames made that necessary.
        build_args += ["--arg", "type", '"vm"']

    # Defaults based on image type
    if template_name is None:
        template_name = "nixos-bootstrap-lxc-latest.tar.xz" if image_type == "lxc" else "nixos-vm-image-x86_64.vma.zst"

    # Content type for PVE storage
    content_type = "vztmpl" if image_type == "lxc" else "images"
    file_ext = "*.tar.xz" if image_type == "lxc" else "*.vma.zst"

    console.print(f"Building NixOS [bold]{image_type.upper()}[/bold] image on [bold]{pve_host}[/bold]...")

    # Build the script to run remotely
    storage_arg = f'"{storage}"' if storage else '""'
    nix_build_args = " ".join(shlex.quote(a) for a in build_args)
    remote_script = f"""
set -euo pipefail

# Source nix
[ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ] && \\
  . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

if ! command -v nix-build >/dev/null 2>&1; then
    echo "ERROR: nix-build not found. Run 'fleet pve install-nix' first." >&2
    exit 1
fi

if [ ! -f "{builder_nix}" ]; then
    echo "ERROR: {builder_nix} not found on this host" >&2
    exit 1
fi

echo "==> Building NixOS {image_type.upper()} image..."
STORE_PATH=$(nix-build "{builder_nix}" --no-out-link {nix_build_args})
echo "==> Store path: $STORE_PATH"

IMAGE=$(find "$STORE_PATH" -maxdepth 2 -name '{file_ext}' | head -n 1)
if [ -z "$IMAGE" ]; then
    echo "ERROR: No {file_ext} found under $STORE_PATH" >&2
    echo "Contents:" >&2
    find "$STORE_PATH" -maxdepth 2 -type f >&2
    exit 1
fi
echo "==> Found image: $IMAGE"

# Detect storage with {content_type} content
PREFERRED={storage_arg}
STORAGE=""
while read -r line; do
    line="$(echo "$line" | sed 's/^[[:space:]]*//')"
    [ -z "$line" ] && continue
    case "$line" in
        dir:*|zfspool:*|lvmthin:*|lvm:*) NAME="${{line#*:}}"; NAME="$(echo "$NAME" | xargs)" ;;
        content*) if echo "$line" | awk '{{print $2}}' | tr ',' '\\n' | grep -q '^{content_type}$'; then
            if [ -n "$PREFERRED" ] && [ "$NAME" = "$PREFERRED" ]; then STORAGE="$NAME"; break; fi
            [ -z "$STORAGE" ] && STORAGE="$NAME"
        fi ;;
    esac
done < /etc/pve/storage.cfg

if [ -z "$STORAGE" ]; then
    echo "ERROR: No storage with {content_type} content found" >&2
    exit 1
fi
echo "==> Using storage: $STORAGE"

DEST_PATH=$(pvesm path "$STORAGE:{content_type}/{template_name}")
mkdir -p "$(dirname "$DEST_PATH")"
cp "$IMAGE" "$DEST_PATH"
echo "==> Image installed: $DEST_PATH"
sha256sum "$DEST_PATH"
"""

    result = subprocess.run(
        _ssh_cmd(pve_host, pve_user) + ["bash", "-c", remote_script],
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]ERROR:[/red] {image_type.upper()} image build failed")
        sys.exit(1)

    console.print(f"[green]{image_type.upper()} image {template_name} installed successfully[/green]")


def _resolve_hydra_url(job_path: str, download_type: str) -> str:
    """Resolve Hydra stable URL → direct download URL."""
    import requests

    stable_url = f"https://hydra.nixos.org/job/nixos/release-25.11/{job_path}/latest/download-by-type/file/{download_type}"
    console.print(f"[dim]Resolving Hydra URL: {stable_url}[/dim]")

    resp = requests.get(stable_url, allow_redirects=True, stream=True, timeout=30)
    resp.raise_for_status()
    final_url = resp.url
    resp.close()

    console.print(f"[dim]Resolved: {final_url}[/dim]")
    return final_url


@pve.command("create-vm-template")
@click.option("--host", default=None, help="PVE host IP")
@click.option("--vmid", default=9000, type=int, help="Template VMID (default: 9000)")
@click.option("--storage", default="local-storage", help="Storage for restored VM disk")
@click.option("--name", "template_name", default="nixos-vm-template", help="Template VM name")
def create_vm_template(host: str | None, vmid: int, storage: str, template_name: str):
    """Download NixOS Proxmox image from Hydra and create a VM template.

    One-time operation. Downloads the official NixOS VMA image,
    restores it as a VM, and converts to a template for cloning.

    The template VMID (default 9000) is referenced by compute.yaml
    via clone.vm_id for VM provisioning.

    Examples:
      fleet bootstraps pve create-vm-template
      fleet bootstraps pve create-vm-template --vmid 9001 --storage local-data
    """
    pve_host, _ = _get_pve_config(host)

    # Resolve latest Hydra VMA URL
    console.print("[bold]Resolving latest NixOS Proxmox image from Hydra...[/bold]")
    vma_url = _resolve_hydra_url("nixos.proxmoxImage.x86_64-linux", "vma")
    vma_filename = vma_url.rsplit("/", 1)[-1]

    # Download + restore + template on PVE
    remote_script = f"""
set -euo pipefail

VMA_URL="{vma_url}"
VMA_FILE="/tmp/{vma_filename}"
VMID={vmid}
STORAGE="{storage}"
NAME="{template_name}"

# Check if template already exists
if qm status $VMID >/dev/null 2>&1; then
    echo "VM $VMID already exists. Destroy it first or use a different --vmid."
    exit 1
fi

# Download
echo "==> Downloading $VMA_URL ..."
wget -q --show-progress -O "$VMA_FILE" "$VMA_URL"

# Restore as VM
echo "==> Restoring VMA to VM $VMID on storage $STORAGE ..."
qmrestore "$VMA_FILE" $VMID --storage "$STORAGE" --force

# Configure for template use
echo "==> Configuring template ..."
qm set $VMID --name "$NAME"
qm set $VMID --agent enabled=1
qm set $VMID --delete unused0 2>/dev/null || true
qm set $VMID --delete ide3 2>/dev/null || true

# Convert to template
echo "==> Converting to template ..."
qm template $VMID

# Cleanup
rm -f "$VMA_FILE"

echo "==> Template VM $VMID ($NAME) ready for cloning"
qm config $VMID | head -15
"""

    console.print(f"Creating VM template [bold]{template_name}[/bold] (VMID {vmid}) on {pve_host}...")
    result = subprocess.run(
        _ssh_cmd(pve_host) + ["bash", "-c", remote_script],
        text=True,
    )
    if result.returncode != 0:
        console.print("[red]ERROR:[/red] Template creation failed")
        sys.exit(1)

    console.print(f"[green]Template VMID {vmid} ready — use clone.vm_id: {vmid} in compute.yaml[/green]")


# Lowest PVE release fleetkit's NixOS LXC path supports: PVE 9 ships
# PVE::LXC::Setup::NixOS, which writes the guest's eth0.network from the
# container's net0 ip=/gw= at create time (ostype = "nixos"), making a
# fresh container reachable on its declared address with no bootstrap
# step. Mirrors fleet.providers.proxmox.<inst>.minVersion (default 9.0).
PVE_MIN_VERSION = (9, 0)


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for p in str(text).split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


@pve.command("status")
@click.option("--node", default=None,
              help="Limit to one cluster node (default: every node).")
def status(node: str | None):
    """Show Proxmox host status and container overview (via REST API).

    Enumerates every node unless --node narrows it. This used to call
    list_containers() bare, and that helper defaults to node="pve" — the
    name the fleet's single PVE host carried before it became a cluster.
    On a multi-node estate "pve" is not a node at all, so the verb died
    with `hostname lookup 'pve' failed` and could not show a container
    even when pointed straight at one.
    """
    from .pve_api import get_client, list_containers

    console.print("Querying PVE API...")

    try:
        api = get_client()
        nodes = [node] if node else [n["node"] for n in api.nodes.get()]
        cts = []
        for n in nodes:
            # A member can be down while the cluster itself answers; one
            # unreachable node should not blank the whole table.
            try:
                for ct in list_containers(api, n):
                    cts.append(dict(ct, node=n))
            except Exception as exc:  # noqa: BLE001 — surface, don't abort
                console.print(f"[yellow]warning:[/yellow] node {n}: {exc}")
    except Exception as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    try:
        ver = api.version.get().get("version", "")
    except Exception:  # noqa: BLE001 — version is informational
        ver = ""
    if ver:
        if _version_tuple(ver) < PVE_MIN_VERSION:
            console.print(
                f"[red]PVE {ver} is below the supported floor "
                f"{'.'.join(map(str, PVE_MIN_VERSION))}[/red] — NixOS containers "
                "will not get their network configured at create time (ostype=nixos "
                "needs the PVE 9 NixOS LXC setup plugin).")
        else:
            console.print(f"[green]PVE {ver}[/green]")

    from rich.table import Table
    table = Table(title="Containers", show_header=True, header_style="bold cyan")
    table.add_column("VMID", justify="right")
    table.add_column("Node", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Status")
    table.add_column("CPU", justify="right")
    table.add_column("Mem (MB)", justify="right")

    for ct in sorted(cts, key=lambda c: int(c.get("vmid", 0))):
        status_style = "green" if ct.get("status") == "running" else "red"
        mem_mb = round(int(ct.get("maxmem", 0)) / 1024 / 1024)
        table.add_row(
            str(ct.get("vmid", "")),
            ct.get("node", ""),
            ct.get("name", ""),
            f"[{status_style}]{ct.get('status', 'unknown')}[/{status_style}]",
            str(ct.get("cpus", "")),
            str(mem_mb),
        )

    console.print(table)
    console.print(f"\n[dim]{len(cts)} containers total[/dim]")


@pve.command("ct")
@click.argument("action", type=click.Choice(["start", "stop", "shutdown", "reboot"]))
@click.argument("vmid", type=int)
@click.option("--wait/--no-wait", default=True,
              help="Block until the PVE task finishes (default: wait).")
def ct(action: str, vmid: int, wait: bool):
    """Start / stop / shutdown / reboot container VMID.

    pve_api has carried these four calls for a long time with nothing
    exposing them, so a container that tofu created but did not leave
    running had no sanctioned way back up — `fleet deploy` would just
    fail to reach it, and the only fix was the web UI or raw SSH to the
    hypervisor.

    The node is resolved from /cluster/resources, so VMID is enough.
    """
    from .pve_api import (get_client, resolve_node, start_container,
                          stop_container, shutdown_container,
                          reboot_container, wait_for_task)

    verbs = {
        "start": start_container,
        "stop": stop_container,
        "shutdown": shutdown_container,
        "reboot": reboot_container,
    }

    try:
        api = get_client()
        node = resolve_node(api, vmid)
        console.print(f"{action} CT {vmid} on [bold]{node}[/bold]…")
        upid = verbs[action](api, vmid, node)
    except Exception as exc:  # noqa: BLE001 — surface the API's own message
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    if not wait:
        console.print(f"[dim]{upid}[/dim]")
        return

    result = wait_for_task(api, upid, node)
    # PVE reports a finished task as status=stopped; whether it WORKED is
    # exitstatus, which is "OK" or an error string. Treating "stopped" as
    # success would report every failed start as a success.
    exit_status = result.get("exitstatus", "")
    if result.get("status") == "timeout":
        console.print(f"[yellow]still running after timeout:[/yellow] {upid}")
        sys.exit(1)
    if exit_status and exit_status != "OK":
        console.print(f"[red]ERROR:[/red] {exit_status}")
        sys.exit(1)
    console.print(f"[green]{action} complete[/green]")


@pve.command("storage")
@click.option("--node", default=None,
              help="Limit to one cluster node (default: every node).")
def storage(node: str | None):
    """Show datastore capacity, and what guests have been PROMISED.

    Thin pools are the fleet's most consequential silent failure: LVM-thin
    and ZFS both hand out volumes lazily, so a `tofu apply` that overcommits
    the pool succeeds, the guest boots, and nothing complains until the pool
    actually fills — at which point EVERY guest sharing it goes read-only at
    once (INFRA-164). `used` alone cannot warn you about that; the number
    that can is the sum of what the guests are entitled to grow into.

    So this reports both: real allocation against capacity, and provisioned
    against capacity. The second exceeding 100% is not itself a fault — it
    is the point of thin provisioning — but it is the number to know before
    adding another large disk to a pool.
    """
    from .pve_api import get_client

    console.print("Querying PVE API...")
    promised: dict[tuple[str, str], int] = {}
    try:
        api = get_client()
        nodes = [node] if node else [n["node"] for n in api.nodes.get()]
        stores = []
        for n in nodes:
            for s in api.nodes(n).storage.get():
                if s.get("total"):
                    stores.append((n, s))
    except Exception as exc:  # noqa: BLE001 — surface the API's own message
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    # /cluster/resources carries guest sizes but not WHICH datastore each
    # disk is on, and that mapping is the whole question here — so the
    # promise total needs the per-guest config. Cheap enough at fleet scale,
    # and skipped for any guest whose config will not read.
    for n in nodes:
        for kind in ("lxc", "qemu"):
            try:
                guests = getattr(api.nodes(n), kind).get()
            except Exception:  # noqa: BLE001 — a node may be down
                continue
            for g in guests:
                try:
                    cfg = getattr(api.nodes(n), kind)(g["vmid"]).config.get()
                except Exception:  # noqa: BLE001
                    continue
                for key, val in cfg.items():
                    if not isinstance(val, str) or ":" not in val:
                        continue
                    if not (key.startswith(("rootfs", "scsi", "virtio",
                                            "sata", "ide", "mp"))):
                        continue
                    ds = val.split(":", 1)[0]
                    for part in val.split(","):
                        if part.startswith("size="):
                            raw = part[5:]
                            mult = {"T": 2**40, "G": 2**30,
                                    "M": 2**20, "K": 2**10}.get(raw[-1:], 1)
                            try:
                                num = float(raw[:-1] if mult > 1 else raw)
                            except ValueError:
                                continue
                            promised[(n, ds)] = promised.get((n, ds), 0) + int(num * mult)

    from rich.table import Table
    table = Table(title="Datastores", show_header=True, header_style="bold cyan")
    table.add_column("Node", style="dim")
    table.add_column("Datastore", style="green")
    table.add_column("Type")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Used %", justify="right")
    table.add_column("Promised", justify="right")
    table.add_column("Promised %", justify="right")

    gib = float(2**30)
    for n, s in sorted(stores, key=lambda x: (x[0], x[1]["storage"])):
        total = int(s["total"])
        used = int(s.get("used", 0))
        pct = used * 100.0 / total
        prom = promised.get((n, s["storage"]), 0)
        prom_pct = prom * 100.0 / total
        style = "red" if pct >= 85 else "yellow" if pct >= 70 else "green"
        # Overcommit is expected on a thin pool; flag it only once the
        # promises are far enough ahead that a fill-up is plausible.
        p_style = "red" if prom_pct >= 200 else "yellow" if prom_pct >= 100 else "green"
        table.add_row(
            n, s["storage"], s.get("type", ""),
            f"{used / gib:.1f}G", f"{total / gib:.1f}G",
            f"[{style}]{pct:.0f}%[/{style}]",
            f"{prom / gib:.1f}G" if prom else "-",
            f"[{p_style}]{prom_pct:.0f}%[/{p_style}]" if prom else "-",
        )

    console.print(table)
