"""fleet tailnet — headscale control-plane operations.

    fleet tailnet nodes
    fleet tailnet approve-routes <node>

Subnet routing takes three independent things, and skipping any one of them
produces a failure that looks exactly like the other two:

  1. the node ADVERTISES the CIDR      (`tailnet_advertise_routes` / the NixOS
                                        module's advertiseRoutes)
  2. an operator APPROVES it           (this command, or the control server's
                                        policy autoApprovers)
  3. the node FORWARDS                 (net.ipv4.ip_forward — handled by the
                                        ansible base role alongside 1)

Step 2 is the one with no declarative home for hosts outside the fleet
manifest: policy `autoApprovers` keys on tags, and the tag reconciler only
tags nodes it can derive from the manifest, so an ansible-managed host has
nothing to match on. That is the gap this group fills.

Prefer `autoApprovers` where it applies — approval belongs in the policy for
anything the manifest describes. Reach for this for the cases it cannot
express, and to SEE state: `nodes` answers "advertised but not approved?",
which is otherwise a shell on the control host.
"""
from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

from . import headscale_api
from .config import get

console = Console()


def _base_url() -> str:
    url = get("tailnet.control_url")
    if not url:
        console.print(
            "[red]ERROR:[/red] fleet.settings.tailnet.controlUrl is not set — "
            "there is no control server to talk to.")
        sys.exit(1)
    return str(url)


def _routes_of(node: dict) -> tuple[list[str], list[str]]:
    """(advertised, approved) for a node, tolerating either API spelling.

    headscale renders JSON names as camelCase through the gRPC gateway, but a
    `jq`-shaped snake_case reply shows up often enough in fixtures and proxies
    that accepting both costs one `or` and saves a silent empty table.
    """
    advertised = node.get("availableRoutes") or node.get("available_routes") or []
    approved = node.get("approvedRoutes") or node.get("approved_routes") or []
    return list(advertised), list(approved)


@click.group("tailnet")
def tailnet() -> None:
    """Headscale control-plane queries and subnet-route approval."""


@tailnet.command("nodes")
@click.option("--json", "as_json", is_flag=True, help="Raw API output.")
@click.option("--pending", is_flag=True,
              help="Only nodes advertising a route that is not approved.")
def nodes_cmd(as_json: bool, pending: bool) -> None:
    """List enrolled nodes with their advertised vs approved routes."""
    import json as _json
    try:
        nodes = headscale_api.list_nodes(_base_url())
    except headscale_api.HeadscaleError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    if pending:
        nodes = [n for n in nodes
                 if set(_routes_of(n)[0]) - set(_routes_of(n)[1])]

    if as_json:
        click.echo(_json.dumps(nodes, indent=2))
        return

    if not nodes:
        console.print("[yellow]no nodes[/yellow]"
                      + (" with unapproved routes" if pending else ""))
        return

    table = Table(title="tailnet nodes")
    table.add_column("node")
    table.add_column("id", justify="right")
    table.add_column("online")
    table.add_column("advertised")
    table.add_column("approved")
    for n in sorted(nodes, key=lambda n: str(n.get("givenName") or n.get("name"))):
        advertised, approved = _routes_of(n)
        # Colour the DIFFERENCE, not the list: an advertised route that nobody
        # approved is the actionable state, and it is invisible in two plain
        # columns of similar-looking CIDRs.
        adv = "\n".join(
            (f"[yellow]{r}[/yellow]" if r not in approved else r) for r in advertised)
        table.add_row(
            str(n.get("givenName") or n.get("name") or "?"),
            str(n.get("id") or ""),
            "[green]yes[/green]" if n.get("online") else "[dim]no[/dim]",
            adv or "[dim]—[/dim]",
            "\n".join(approved) or "[dim]—[/dim]",
        )
    console.print(table)
    console.print("[dim]yellow = advertised but not approved[/dim]")


@tailnet.command("approve-routes")
@click.argument("node")
@click.option("--route", "routes", multiple=True, metavar="CIDR",
              help="Approve only this route (repeatable). Default: all advertised.")
@click.option("--replace", is_flag=True,
              help="Set approved routes to exactly --route, dropping any other "
                   "currently-approved route. Requires --route.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
def approve_routes_cmd(node: str, routes: tuple[str, ...], replace: bool,
                       yes: bool) -> None:
    """Approve the subnet routes a NODE advertises.

    \b
      fleet tailnet approve-routes dell-2
      fleet tailnet approve-routes dell-2 --route 10.43.0.0/24

    The API call REPLACES a node's approved set, so by default this sends the
    union of what is already approved and what is being added — approving one
    route never silently withdraws another. `--replace` opts into the
    destructive form deliberately.
    """
    if replace and not routes:
        console.print("[red]ERROR:[/red] --replace needs at least one --route "
                      "(it would otherwise un-approve everything).")
        sys.exit(1)

    base = _base_url()
    try:
        entry = headscale_api.find_node(base, node)
    except headscale_api.HeadscaleError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    advertised, approved = _routes_of(entry)
    node_id = str(entry.get("id"))

    wanted = list(routes) if routes else advertised
    unadvertised = [r for r in wanted if r not in advertised]
    if unadvertised:
        # Approving a route the node does not advertise is accepted by the API
        # and does nothing observable — it lands in approvedRoutes, never in
        # subnetRoutes. Refuse rather than report a success that carries no
        # traffic; a typo'd CIDR is the usual cause.
        console.print(
            f"[red]ERROR:[/red] {node} does not advertise: {', '.join(unadvertised)}\n"
            f"advertised: {', '.join(advertised) or '(none)'}\n"
            "Advertise it on the node first (tailnet_advertise_routes / "
            "infra.network.tailnet.advertiseRoutes), then approve.")
        sys.exit(1)

    target = sorted(set(wanted)) if replace else sorted(set(approved) | set(wanted))
    if target == sorted(set(approved)):
        console.print(f"[green]{node}[/green]: already approved "
                      f"({', '.join(target) or 'nothing advertised'}) — nothing to do.")
        return

    adding = sorted(set(target) - set(approved))
    dropping = sorted(set(approved) - set(target))
    console.print(f"[bold]{node}[/bold] (id {node_id})")
    if adding:
        console.print(f"  [green]+ {', '.join(adding)}[/green]")
    if dropping:
        console.print(f"  [red]- {', '.join(dropping)}[/red]")

    if not yes and not click.confirm("apply?", default=False):
        console.print("aborted")
        sys.exit(1)

    try:
        headscale_api.approve_routes(base, node_id, target)
    except headscale_api.HeadscaleError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)
    console.print(f"[green]approved[/green]: {', '.join(target)}")
