"""fleet pve cluster — declarative-ish PVE cluster lifecycle.

Reads the tier-1 PVE hosts from .cache/fleet/hosts.json (derived from
fleet.compute) and converges their cluster membership using `pvecm`.

The founder is the entry tagged "founder" (currently `pve-platform`).
Joiners are every other entry tagged "pve-host". PBS (tagged "pbs-host")
is excluded — PBS isn't part of a PVE cluster.

SSH trust model:
- The sysadmin SSH pubkey is already in every tier-1 host's
  /root/.ssh/authorized_keys (planted at install time via the answer.toml
  `root-ssh-keys` field rendered by infra.provisioning.pveInstallerAnswers).
- The operator's ssh-agent is forwarded with `-A`. When a joiner runs
  `pvecm add <founder> --use_ssh`, the internal ssh from joiner→founder
  picks up the forwarded agent and authenticates as the operator's key.
- No shared cluster bootstrap keys, no SOPS-encrypted keypairs, no
  HTTP-served secrets. The sysadmin's trust root does the work.

Idempotent. Re-running `converge` is safe:
- founder already-clustered with correct name → skipped
- founder already-clustered with WRONG name → error (user must intervene)
- joiner already a member → skipped
- joiner not a member → `pvecm add <founder-ip> --use_ssh`

Run after the tier-1 PVE installs land — typically once at fleet
bootstrap, then ad-hoc when a new PVE host gets installed later.

`issue-tf-token` is the exception to the founder model above: it mints an
API token on whichever node --node / --address names (founder by default),
because a node that terranix does not manage yet has no fleet.compute entry
to be the founder *of*. It talks to `pveum` only, so it works on a
standalone node with no corosync at all.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import click
from rich.console import Console
from rich.table import Table

from ._util import find_project_root, fleet_cache_dir, fleet_executable

console = Console()

DEFAULT_CLUSTER_NAME = "fleet-pve"
SSH_CONNECT_TIMEOUT = 10


def _load_hosts() -> dict[str, dict]:
    root = find_project_root()
    hosts_file = fleet_cache_dir(root) / "hosts.json"
    if not hosts_file.exists():
        console.print(
            "[red]ERROR:[/red] .cache/fleet/hosts.json not found — "
            "run `fleet inventory generate` first"
        )
        sys.exit(1)
    with open(hosts_file) as f:
        return json.load(f)


def _pve_cluster_members(hosts: dict[str, dict]) -> dict[str, dict]:
    """Tier-1 PVE hosts that should be in the cluster. Excludes PBS."""
    return {
        name: meta
        for name, meta in hosts.items()
        if "pve-host" in meta.get("tags", [])
    }


def _split_founder(pve_hosts: dict[str, dict]) -> tuple[tuple[str, dict], list[tuple[str, dict]]]:
    """Return (founder, joiners). Errors if 0 or >1 founders are tagged."""
    founders = [(n, m) for n, m in pve_hosts.items() if "founder" in m.get("tags", [])]
    if not founders:
        console.print(
            "[red]ERROR:[/red] no tier-1 PVE host carries the 'founder' tag in fleet.compute"
        )
        sys.exit(1)
    if len(founders) > 1:
        names = ", ".join(n for n, _ in founders)
        console.print(f"[red]ERROR:[/red] multiple founders tagged: {names}")
        sys.exit(1)
    founder = founders[0]
    joiners = [(n, m) for n, m in pve_hosts.items() if n != founder[0]]
    joiners.sort(key=lambda nm: nm[0])
    return founder, joiners


def _resolve_token_target(node: str | None, address: str | None) -> tuple[str, str]:
    """Pick the machine to mint an API token on. Returns (label, address).

    Three ways in, in precedence order:

    ``--address``
        An arbitrary reachable PVE node; the catalog is never consulted
        (hosts.json need not even exist). This is the bootstrap case: a
        node terranix does not manage yet has no fleet.compute entry, and
        the token is precisely what lets it acquire one.
    ``--node``
        A "pve-host"-tagged fleet.compute entry, by name. Uses its
        internal_ip. Pass --address instead when the API is reachable at
        some other address than the manifest's (a second site behind a
        VPN, say).
    neither
        The "founder"-tagged entry — the historical behaviour, kept so
        existing invocations do not change meaning.

    Token minting is deliberately NOT restricted to the founder: it grants
    Administrator on / and is therefore the one command here where hitting
    the wrong machine is expensive. Whichever way the target is chosen, the
    caller prints it before the confirmation prompt.
    """
    if node and address:
        console.print(
            "[red]ERROR:[/red] pass --node or --address, not both — "
            "they name the same thing two different ways."
        )
        sys.exit(1)

    if address:
        return address, address

    pve_hosts = _pve_cluster_members(_load_hosts())
    if not pve_hosts:
        console.print(
            "[red]ERROR:[/red] no 'pve-host'-tagged entries in fleet.compute. "
            "Use --address <ip> to mint against a node that is not in the "
            "manifest yet."
        )
        sys.exit(1)

    if node:
        meta = pve_hosts.get(node)
        if meta is None:
            known = ", ".join(sorted(pve_hosts))
            console.print(
                f"[red]ERROR:[/red] --node {node!r} is not a 'pve-host'-tagged "
                f"entry in fleet.compute. Known: {known}. Use --address <ip> "
                "for a node outside the manifest."
            )
            sys.exit(1)
        return node, meta["internal_ip"]

    (founder_name, founder_meta), _ = _split_founder(pve_hosts)
    return founder_name, founder_meta["internal_ip"]


def _check_ssh_agent() -> None:
    """Bail early if the operator hasn't got an ssh-agent up."""
    if not os.environ.get("SSH_AUTH_SOCK"):
        console.print(
            "[red]ERROR:[/red] no SSH_AUTH_SOCK in env. Start ssh-agent and "
            "add your sysadmin key:\n"
            "  eval $(ssh-agent)\n"
            "  ssh-add ~/.ssh/id_ed25519"
        )
        sys.exit(1)


def _ssh(ip: str, remote_cmd: str, *, timeout: int = SSH_CONNECT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a single command on a host with the operator's agent forwarded.

    Always returns CompletedProcess (check=False) so callers can branch
    on stdout/stderr/exit code without exception plumbing.
    """
    cmd = [
        "ssh",
        "-A",
        "-o", "ForwardAgent=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "BatchMode=yes",
        f"root@{ip}",
        remote_cmd,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _cluster_state(ip: str) -> dict:
    """Probe a host's cluster state.

    Returns:
        {
          "reachable": bool,
          "in_cluster": bool,
          "cluster_name": Optional[str],
          "raw_output": str,
          "error": Optional[str],
        }
    """
    ping = _ssh(ip, "true", timeout=5)
    if ping.returncode != 0:
        return {
            "reachable": False, "in_cluster": False, "cluster_name": None,
            "raw_output": "", "error": ping.stderr.strip() or "ssh failed",
        }

    res = _ssh(ip, "pvecm status 2>&1 || true")
    out = res.stdout
    if "Cluster information" in out and "Name:" in out:
        # Parse "Name:             <cluster name>"
        name = None
        for line in out.splitlines():
            if line.strip().startswith("Name:"):
                name = line.split(":", 1)[1].strip()
                break
        return {
            "reachable": True, "in_cluster": True, "cluster_name": name,
            "raw_output": out, "error": None,
        }
    return {
        "reachable": True, "in_cluster": False, "cluster_name": None,
        "raw_output": out, "error": None,
    }


def _render_state_table(state_by_name: dict[str, dict], founder_name: str) -> Table:
    table = Table(title="PVE cluster state", show_lines=False)
    table.add_column("host", style="bold")
    table.add_column("role")
    table.add_column("ip")
    table.add_column("reachable")
    table.add_column("in cluster")
    table.add_column("cluster name")
    table.add_column("note", overflow="fold")

    for name in sorted(state_by_name.keys()):
        s = state_by_name[name]
        role = "founder" if name == founder_name else "joiner"
        table.add_row(
            name,
            role,
            s["ip"],
            "[green]yes[/]" if s["reachable"] else "[red]no[/]",
            "[green]yes[/]" if s["in_cluster"] else "[yellow]no[/]",
            s["cluster_name"] or "—",
            s["error"] or "",
        )
    return table


# ── Click group + commands ───────────────────────────────────────────


@click.group("cluster")
def cluster() -> None:
    """PVE cluster lifecycle (create / add nodes / inspect).

    Operates on tier-1 PVE hosts from fleet.compute (entries tagged
    "pve-host"). The "founder" tag picks the primary node.
    """


@cluster.command("status")
def status() -> None:
    """Show pvecm status across all tier-1 PVE hosts.

    Read-only. Prints a table of reachability + cluster membership +
    cluster name per host. Useful for spotting drift between declared
    and actual cluster state.
    """
    _check_ssh_agent()
    hosts = _load_hosts()
    pve_hosts = _pve_cluster_members(hosts)
    if not pve_hosts:
        console.print("[yellow]No tier-1 PVE hosts found in fleet.compute[/]")
        return

    (founder_name, _), _ = _split_founder(pve_hosts)

    state_by_name: dict[str, dict] = {}
    for name, meta in pve_hosts.items():
        ip = meta["internal_ip"]
        console.print(f"  probing {name} ({ip})...", style="dim")
        st = _cluster_state(ip)
        st["ip"] = ip
        state_by_name[name] = st

    console.print(_render_state_table(state_by_name, founder_name))


@cluster.command("converge")
@click.option(
    "--cluster-name", "cluster_name",
    default=DEFAULT_CLUSTER_NAME,
    show_default=True,
    help="Name to use when creating the cluster (corosync ring name).",
)
@click.option("--dry-run", is_flag=True, help="Print what would happen; make no changes.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def converge(cluster_name: str, dry_run: bool, yes: bool) -> None:
    """Bring the PVE cluster to the declared state.

    Idempotent:
      - founder not in any cluster   → `pvecm create <cluster-name>`
      - founder in <cluster-name>    → skipped
      - founder in a different name  → error, bail
      - joiner already a member      → skipped
      - joiner not in any cluster    → `pvecm add <founder-ip> --use_ssh`
      - joiner in a different cluster → error, bail

    Trust path: the operator's ssh-agent is forwarded with -A. The
    sysadmin SSH key (already in every tier-1 host's authorized_keys
    via the answer.toml install) authenticates each hop, including the
    joiner→founder hop inside `pvecm add`.
    """
    _check_ssh_agent()
    hosts = _load_hosts()
    pve_hosts = _pve_cluster_members(hosts)
    if not pve_hosts:
        console.print("[yellow]No tier-1 PVE hosts found in fleet.compute[/]")
        return

    (founder_name, founder_meta), joiners = _split_founder(pve_hosts)
    founder_ip = founder_meta["internal_ip"]

    console.print(f"[bold]Founder:[/bold] {founder_name} ({founder_ip})")
    console.print(f"[bold]Joiners:[/bold] {', '.join(n for n, _ in joiners) or '(none)'}")
    console.print(f"[bold]Cluster:[/bold] {cluster_name}")
    if dry_run:
        console.print("[yellow](dry-run — no changes will be made)[/]")
    console.print()

    # ── Phase 1: probe state ───────────────────────────────
    state_by_name: dict[str, dict] = {}
    for name, meta in pve_hosts.items():
        ip = meta["internal_ip"]
        console.print(f"  probing {name} ({ip})...", style="dim")
        st = _cluster_state(ip)
        st["ip"] = ip
        state_by_name[name] = st

    console.print(_render_state_table(state_by_name, founder_name))
    console.print()

    # ── Phase 2: plan ──────────────────────────────────────
    plan: list[tuple[str, str, str]] = []  # (host_name, ip, cmd)

    f_state = state_by_name[founder_name]
    if not f_state["reachable"]:
        console.print(
            f"[red]ERROR:[/red] founder {founder_name} ({founder_ip}) is unreachable. "
            "Bring it up before running converge."
        )
        sys.exit(1)
    if f_state["in_cluster"]:
        if f_state["cluster_name"] == cluster_name:
            console.print(f"  founder {founder_name}: already in cluster '{cluster_name}' [green]✓[/]")
        else:
            console.print(
                f"[red]ERROR:[/red] founder {founder_name} is in cluster "
                f"'{f_state['cluster_name']}' but converge wants '{cluster_name}'. "
                "Refusing to act. Either pass --cluster-name to match, or destroy "
                "the existing cluster on the founder with `pvecm delnode`/`pvecm leave`."
            )
            sys.exit(1)
    else:
        plan.append((founder_name, founder_ip, f"pvecm create {cluster_name}"))

    for joiner_name, joiner_meta in joiners:
        st = state_by_name[joiner_name]
        ip = joiner_meta["internal_ip"]
        if not st["reachable"]:
            console.print(
                f"  joiner {joiner_name}: [yellow]unreachable, skipping[/] "
                f"(re-run converge after it comes up)"
            )
            continue
        if st["in_cluster"]:
            if st["cluster_name"] == cluster_name:
                console.print(f"  joiner {joiner_name}: already a member [green]✓[/]")
            else:
                console.print(
                    f"[red]ERROR:[/red] joiner {joiner_name} is in cluster "
                    f"'{st['cluster_name']}', not '{cluster_name}'. Refusing to act."
                )
                sys.exit(1)
            continue
        plan.append((joiner_name, ip, f"pvecm add {founder_ip} --use_ssh"))

    if not plan:
        console.print("[green]Cluster is already converged. Nothing to do.[/]")
        return

    # ── Phase 3: confirm + apply ──────────────────────────
    console.print()
    console.print("[bold]Planned actions:[/bold]")
    for host_name, ip, cmd in plan:
        console.print(f"  {host_name} ({ip}): [cyan]{cmd}[/]")
    console.print()

    if dry_run:
        return

    if not yes:
        if not click.confirm("Proceed?", default=False):
            console.print("aborted")
            sys.exit(1)

    for host_name, ip, cmd in plan:
        console.print(f"[bold]→ {host_name}:[/bold] {cmd}")
        # pvecm create/add can take 30s+; bump the SSH connect timeout
        # window and let the inner command run unbounded.
        res = _ssh(ip, cmd, timeout=30)
        if res.returncode != 0:
            console.print(f"[red]FAILED on {host_name}[/] (exit {res.returncode})")
            if res.stdout:
                console.print(f"  stdout: {res.stdout.strip()}")
            if res.stderr:
                console.print(f"  stderr: {res.stderr.strip()}")
            sys.exit(res.returncode)
        if res.stdout.strip():
            for line in res.stdout.strip().splitlines():
                console.print(f"  {line}", style="dim")
        console.print(f"  [green]✓[/]")

    console.print()
    console.print("[green]Converge complete. Re-run `fleet pve cluster status` to verify.[/]")


@cluster.command("issue-tf-token")
@click.option("--node", "node", default=None,
              help="Mint on this 'pve-host'-tagged fleet.compute entry instead "
                   "of the cluster founder. Its internal_ip is used for both "
                   "SSH and the saved endpoint.")
@click.option("--address", "address", default=None,
              help="Mint on the node at this IP/hostname without consulting the "
                   "fleet catalog — for a node terranix does not manage yet, or "
                   "one reachable at a different address than its internal_ip. "
                   "Mutually exclusive with --node.")
@click.option("--user", "username", default="terranix@pve", show_default=True,
              help="PVE user to create / use. Use @pve (PVE-managed) not @pam: "
                   "PAM realm requires a Linux user in /etc/passwd, which pveum "
                   "won't create for you.")
@click.option("--token-id", "token_id", default="terranix", show_default=True,
              help="API token ID to mint.")
@click.option("--sops-prefix", default=f"integrations/proxmox/{DEFAULT_CLUSTER_NAME}", show_default=True,
              help="SOPS path prefix for endpoint + api_token.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def issue_tf_token(node: str | None, address: str | None, username: str,
                   token_id: str, sops_prefix: str, yes: bool) -> None:
    """Mint a Proxmox API token for terranix on a PVE node.

    Targets the cluster founder by default; --node picks another entry from
    fleet.compute, --address any reachable node at all. That last one is the
    bootstrap case and the reason this command is not founder-only: a node
    terranix does not manage yet has no catalog entry, and this token is what
    lets it get one. A standalone node with no cluster works the same way —
    `pveum` does not need corosync.

    Idempotent steps on the target:
      1. Create `<username>` if absent (random password — never used, the
         token is what terraform talks to the API with).
      2. Grant Administrator on / to the user (so terranix can do anything
         the existing pveum + API model allows).
      3. Remove any existing `<token_id>` token (so we can capture a fresh
         secret value), then create it with --privsep=0.
      4. Capture the API JSON which carries the `value` (UUID) + the
         `full-tokenid` (e.g. terranix@pve!terranix).
      5. Save to SOPS:
           <sops-prefix>/endpoint   = https://<target>:8006
           <sops-prefix>/api_token  = <full-tokenid>=<value>
         The api_token string matches what bpg/proxmox expects in its
         `api_token` provider attribute.

    Pass --sops-prefix per provider instance; the default is shared and two
    instances writing the same prefix would clobber each other's credentials.

    The terranix provider config should then reference
    `secrets.api_token = "<sops-prefix>/api_token"` and the printed endpoint.
    """
    _check_ssh_agent()
    target_name, target_address = _resolve_token_target(node, address)
    endpoint = f"https://{target_address}:8006"

    console.print(f"[bold]Target:[/bold]  {target_name} ({endpoint})")
    console.print(f"[bold]User:[/bold]    {username}")
    console.print(f"[bold]Token:[/bold]   {token_id}")
    console.print(f"[bold]SOPS:[/bold]    {sops_prefix}/{{endpoint,api_token}}")
    console.print()

    if not yes and not click.confirm("Mint token + save to SOPS?", default=False):
        console.print("aborted")
        sys.exit(1)

    # ── Run on the target ─────────────────────────────────
    # Bash heredoc so the multi-step script runs as a single SSH op.
    remote = f"""
set -euo pipefail

# 1. user (idempotent — pveum user add fails noisily if exists, so check first)
if ! pveum user list --output-format json 2>/dev/null | python3 -c "import json,sys; sys.exit(0 if any(u.get('userid')=='{username}' for u in json.load(sys.stdin)) else 1)" 2>/dev/null; then
  pw=$(openssl rand -base64 32)
  pveum user add {username} --password "$pw" --comment 'Terranix automation user'
fi

# 2. role: Administrator on / (idempotent — re-running is a no-op)
pveum aclmod / --user {username} --role Administrator

# 3. token: remove if exists so we capture a fresh secret, then create
if pveum user token list {username} --output-format json 2>/dev/null | python3 -c "import json,sys; sys.exit(0 if any(t.get('tokenid')=='{token_id}' for t in json.load(sys.stdin)) else 1)" 2>/dev/null; then
  pveum user token remove {username} {token_id} >/dev/null
fi
pveum user token add {username} {token_id} --privsep=0 --output-format json
"""

    console.print(f"  running on {target_name}...", style="dim")
    res = _ssh(target_address, remote, timeout=30)
    if res.returncode != 0:
        console.print(f"[red]FAILED on {target_name}[/] (exit {res.returncode})")
        if res.stderr:
            console.print(f"  stderr: {res.stderr.strip()}")
        sys.exit(res.returncode)

    # Parse output. pveum --output-format json prints the token object
    # on a single line (the last non-empty line of stdout) — the embedded
    # "info" attr has its own {...} so a naive `rfind('{')` lands inside
    # that. Walk lines from the bottom and pick the first one that
    # round-trips through json.loads.
    import json as _json
    payload = None
    for line in reversed(res.stdout.splitlines()):
        s = line.strip()
        if not s or not s.startswith("{"):
            continue
        try:
            payload = _json.loads(s)
            break
        except _json.JSONDecodeError:
            continue
    if payload is None:
        console.print(f"[red]Could not find JSON in pveum output[/]:\n{res.stdout}")
        sys.exit(1)

    secret_value = payload.get("value")
    full_token_id = payload.get("full-tokenid")
    if not secret_value or not full_token_id:
        console.print(f"[red]Missing value/full-tokenid in payload[/]: {payload}")
        sys.exit(1)

    api_token = f"{full_token_id}={secret_value}"
    console.print(f"  ✓ minted token [cyan]{full_token_id}[/]")

    # ── Save to SOPS ──────────────────────────────────────
    # Shell out to `fleet devtools secrets keys add` since that already
    # handles the SOPS plumbing (age key, file structure, etc.).
    for path, value in [(f"{sops_prefix}/endpoint", endpoint),
                        (f"{sops_prefix}/api_token", api_token)]:
        sk_res = subprocess.run(
            [fleet_executable(), "devtools", "secrets", "keys", "add", path, value],
            capture_output=True, text=True, check=False,
        )
        if sk_res.returncode != 0:
            console.print(f"[red]Failed to save {path}[/]: {sk_res.stderr.strip()}")
            sys.exit(sk_res.returncode)
        console.print(f"  ✓ wrote SOPS: [cyan]{path}[/]")

    console.print()
    console.print("[green]Token minted + saved to SOPS.[/]")
    console.print()
    console.print("[bold]Next:[/bold] wire the provider in nix/fleet/providers/inputs.nix to use:")
    console.print(f'  endpoint = "{endpoint}";')
    console.print(f'  secrets.api_token = "{sops_prefix}/api_token";')
    console.print("then `fleet deploy tf init <stack>` against the new provider instance.")
