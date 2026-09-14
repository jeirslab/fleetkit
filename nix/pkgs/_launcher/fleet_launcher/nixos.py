"""fleet nixos — NixOS/Colmena deployment commands.

Wraps Colmena CLI for declarative NixOS configuration management
across the LXC container fleet. Organized into apply/build/eval/list
subgroups with consistent host/stack/tag targeting.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import click
from rich.console import Console
from rich.table import Table

from ._util import find_project_root, fleet_cache_dir, run_shell

console = Console()


def _refresh_inventory() -> None:
    """Regenerate hosts.json from PVE API before Colmena runs."""
    from .inventory import generate_hosts_json
    console.print("[dim]Refreshing hosts.json from PVE API...[/dim]")
    try:
        generate_hosts_json(quiet=True)
        console.print("[dim]hosts.json updated[/dim]")
    except SystemExit:
        console.print("[yellow]Warning:[/yellow] Could not refresh hosts.json — using existing file")
    except Exception as exc:
        console.print(f"[yellow]Warning:[/yellow] Inventory refresh failed: {exc} — using existing file")

    # Check if the remote Nix builder is reachable
    _check_remote_builder()


def _check_no_concurrent_deploy(host_names: tuple[str, ...] | None = None) -> None:
    """Abort if a deploy is already in flight targeting any of the given hosts.

    Passing ``None`` scans for any deploy at all (used for `apply all` / `apply
    stack` / `apply tag` where the exact target list isn't enumerable upfront).

    Different hosts running simultaneously is fine — this only blocks when the
    same IP is already being copied to.
    """
    from .reset_connection import (
        _find_broad_processes,
        _find_processes_for_ip,
        _ips_for,
        _load_hosts,
        _ps_snapshot,
    )

    snap = _ps_snapshot()

    conflicts: dict[str, list[dict]] = {}
    if host_names:
        hosts = _load_hosts()
        for name in host_names:
            meta = hosts.get(name)
            if not meta:
                continue
            procs: list[dict] = []
            seen = set()
            for ip in _ips_for(meta):
                for p in _find_processes_for_ip(ip, snap):
                    if p["pid"] not in seen:
                        seen.add(p["pid"])
                        procs.append(p)
            if procs:
                conflicts[name] = procs
    else:
        broad = _find_broad_processes(snap)
        if broad:
            conflicts["<any host>"] = broad

    if not conflicts:
        return

    console.print()
    console.print("[red bold]Deploy conflict:[/red bold] another deploy is already in flight.")
    for name, procs in conflicts.items():
        console.print(f"  [yellow]{name}[/yellow]:")
        for p in procs:
            cmd_preview = p["cmd"][:100] + ("…" if len(p["cmd"]) > 100 else "")
            console.print(f"    PID [cyan]{p['pid']}[/cyan]  {cmd_preview}")
    console.print()
    console.print("Options:")
    console.print("  • [cyan]wait[/cyan] for the existing deploy to finish")
    if host_names:
        host_arg = " ".join(host_names) if len(host_names) == 1 else ""
        console.print(f"  • [cyan]fleet devtools reset-connection {host_arg}[/cyan] — clear orphans and retry")
    else:
        console.print("  • [cyan]fleet devtools reset-connection --broad[/cyan] — clear all deploy orphans")
    console.print()
    sys.exit(2)


def _check_remote_builder() -> None:
    """Check if the remote Nix builder (tagged 'builder') is SSH-reachable."""
    root = find_project_root()
    hosts_file = fleet_cache_dir(root) / "hosts.json"
    if not hosts_file.exists():
        return

    try:
        with open(hosts_file) as f:
            hosts = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    # Find builder IP
    builder_ip = None
    for _name, meta in hosts.items():
        if "builder" in (meta.get("tags") or []) and meta.get("ip"):
            builder_ip = meta["ip"]
            break

    if not builder_ip:
        console.print("[dim]No builder host found in inventory[/dim]")
        return

    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
         f"root@{builder_ip}", "true"],
        capture_output=True,
    )
    if result.returncode == 0:
        console.print(f"[green]Remote builder[/green] [dim]({builder_ip})[/dim] [green]online[/green]")
    else:
        console.print(f"[yellow]Remote builder[/yellow] [dim]({builder_ip})[/dim] [yellow]unreachable — builds will run locally[/yellow]")


@click.group("nixos")
def nixos():
    """NixOS configuration deployment via Colmena.

    Colmena manages the full NixOS container fleet declaratively.
    All commands pass --impure to allow reading runtime state
    (e.g. hosts.json, secrets) during evaluation.

    \b
    Subgroups:
      apply   Deploy NixOS closures to hosts
      build   Build closures without deploying
      eval    Inspect NixOS configuration
      list    Enumerate hosts and metadata
    """


# ── apply ─────────────────────────────────────────────────────

@nixos.group("apply")
def apply():
    """Deploy NixOS closures to hosts.

    Push and activate NixOS system configurations via Colmena.
    Target by host name, stack, or tag.
    """


@apply.command("all")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--no-refresh", is_flag=True, help="Skip hosts.json refresh from PVE API.")
def apply_all(args: tuple[str, ...], no_refresh: bool):
    """Deploy NixOS config to ALL hosts in the fleet.

    Refreshes hosts.json from PVE, then runs ``colmena apply``
    across every deployable node. Extra ARGS are forwarded.
    """
    if not no_refresh:
        _refresh_inventory()
    _check_no_concurrent_deploy()
    run_shell(["colmena", "apply", "--impure", *args],
              interactive=True, log_label="deploy-all")


@apply.command("host")
@click.argument("names", nargs=-1, required=True)
@click.option("--ip", default=None, help="Override target IP for deployment (e.g. reach the host over another network when its primary IP is unreachable).")
@click.option("--no-refresh", is_flag=True, help="Skip hosts.json refresh from PVE API.")
@click.option("--no-session", is_flag=True, help="Run inline instead of a detached background job (for CI / scripts).")
@click.option("--wait", is_flag=True,
              help="Run the per-host jobs and BLOCK until they finish, exiting "
                   "non-zero if any host failed. The honest, scriptable path — "
                   "what CI / the CD runner use (vs the default fire-and-forget "
                   "dispatch, which returns as soon as the jobs are launched).")
@click.option("--reboot", is_flag=True,
              help="Stage as `boot` goal and reboot the target after activation. "
                   "Required when critical components change (dbus-implementation, "
                   "kernel, init system) — NixOS refuses to switch live then.")
@click.option("--dry-activate", "dry_activate", is_flag=True,
              help="Build and copy the closure, then show what activation WOULD "
                   "do (which units restart/reload) without switching. Read-only "
                   "on the target's running system. Mutually exclusive with --reboot.")
def apply_host(names: tuple[str, ...], ip: str | None, no_refresh: bool, no_session: bool,
               wait: bool, reboot: bool, dry_activate: bool):
    """Deploy NixOS config to one or more hosts.

    NAMES are Colmena node names (e.g. ``netgate build auth``).
    Each host runs as its own detached background job by default so
    multiple deploys can run concurrently. Inspect with:

      fleet sessions list
      fleet sessions logs fleet-deploy-<name> -f

    Use --wait to block until the jobs finish and exit with the real
    aggregate result (CI / the CD runner). Use --no-session to run inline.
    Use --ip to override the default hosts.json IP.
    """
    # Auto dispatch: launch one detached native job per host, then return
    # (or, with --wait, run them and block on the real result). Each job
    # re-enters this command with --no-session so the inner copy runs inline.
    from ._util import env_get, fleet_executable
    from .sessions import dispatch_session, run_and_wait, running_inside

    def _inner_cmd(name: str) -> list[str]:
        # Absolute path to this fleet binary: a detached job's env /
        # devshell PATH don't reliably carry `fleet`. If colmena is missing
        # in the job env, the inner fleet re-execs via `nix develop` itself
        # (see main._maybe_reexec_for_missing_tools).
        c = [fleet_executable(), "deploy", "nixos", "apply", "host", name, "--no-session"]
        if ip:
            c += ["--ip", ip]
        if no_refresh:
            c.append("--no-refresh")
        if reboot:
            c.append("--reboot")
        if dry_activate:
            c.append("--dry-activate")
        return c

    backgrounding = not no_session and not env_get("FLEET_NO_SESSION")
    inside_a_job = any(running_inside(f"fleet-deploy-{n}") for n in names)

    # --wait: run the per-host jobs synchronously and exit with the real
    # aggregate result. The honest path CI / the CD runner rely on.
    if wait and backgrounding and not inside_a_job:
        specs = [{
            "name": f"fleet-deploy-{name}",
            "cmd": _inner_cmd(name),
            "cwd": find_project_root(),
            "description": f"Colmena deploy to {name}",
        } for name in names]
        sys.exit(run_and_wait(specs))

    if backgrounding:
        dispatched_any = False
        failed: list[tuple[str, int]] = []
        for name in names:
            session_name = f"fleet-deploy-{name}"
            if running_inside(session_name):
                # Inner job — fall through to inline deploy below.
                continue
            result = dispatch_session(
                session_name,
                _inner_cmd(name),
                cwd=find_project_root(),
                description=f"Colmena deploy to {name}",
            )
            if result == 0:
                dispatched_any = True
            elif result == -1:
                # Already inside the job — let this one fall through.
                break
            else:
                failed.append((name, result))
        if failed:
            console.print(
                f"[red]dispatch failed for:[/red] "
                f"{', '.join(f'{n} (rc={rc})' for n, rc in failed)}"
            )
            sys.exit(1)
        if dispatched_any:
            return  # Dispatched successfully; outer shell returns.

    if not no_refresh:
        _refresh_inventory()
    _check_no_concurrent_deploy(names)
    if ip and len(names) == 1:
        # Patch hosts.json temporarily with override IP
        import json
        root = find_project_root()
        hosts_file = fleet_cache_dir(root) / "hosts.json"
        with open(hosts_file) as f:
            hosts = json.load(f)
        name = names[0]
        if name in hosts:
            original_ip = hosts[name].get("ip", "")
            hosts[name]["ip"] = ip
            with open(hosts_file, "w") as f:
                json.dump(hosts, f, indent=2)
                f.write("\n")
            console.print(f"[yellow]IP override:[/yellow] {name} → {ip} (was {original_ip})")
    selector = ",".join(names)
    # `dry-activate` is a colmena GOAL, not a flag: it builds and copies the
    # closure, then runs the activation script in dry mode so the target
    # reports which units would restart — without switching. The running
    # system is untouched, which makes it the right last check before a real
    # apply, and the reason a deploy CLI should expose it rather than making
    # operators reach for raw colmena.
    if dry_activate and reboot:
        raise click.UsageError("--dry-activate and --reboot are mutually exclusive: "
                               "a dry run does not activate, so there is nothing to reboot into.")
    # `colmena apply [goal]` — the goal is a POSITIONAL argument to apply
    # (build|push|switch|boot|test|dry-activate|keys), not a subcommand.
    cmd = ["colmena", "apply", *(["dry-activate"] if dry_activate else []),
           "--impure", "--on", selector]
    if reboot:
        cmd.append("--reboot")
    run_shell(cmd, interactive=True, log_label=f"deploy-host-{selector}")


@apply.command("remote")
@click.argument("names", nargs=-1, required=True)
@click.option("--reboot", is_flag=True,
              help="Stage the new config as `boot` goal and reboot the target. "
                   "Required when critical components (dbus, kernel, init system) "
                   "change — NixOS refuses to switch live in those cases.")
@click.option("--force-replace-unknown-profiles", is_flag=True,
              help="Allow replacing an active profile that Colmena doesn't recognise "
                   "(e.g. the `-unnamed-` profile from a fresh bootstrap-template VM). "
                   "Required for the first deploy onto a freshly-provisioned host.")
def apply_remote(names: tuple[str, ...], reboot: bool, force_replace_unknown_profiles: bool):
    """Deploy NixOS config, building on the target hosts.

    NAMES are Colmena node names. Useful when the local machine
    lacks build capacity — each target compiles its own closure.
    """
    _refresh_inventory()
    _check_no_concurrent_deploy(names)
    selector = ",".join(names)
    cmd = ["colmena", "apply", "--impure", "--on", selector, "--build-on-target"]
    if reboot:
        cmd.append("--reboot")
    if force_replace_unknown_profiles:
        cmd.append("--force-replace-unknown-profiles")
    run_shell(cmd, interactive=True, log_label=f"deploy-remote-{selector}")


@apply.command("stack")
@click.argument("stack")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def apply_stack(stack: str, args: tuple[str, ...]):
    """Deploy NixOS config to all hosts in a logical stack.

    STACK is a Colmena deployment tag representing a layer
    (e.g. ``platform``, ``data``, ``services``, ``observability``).
    Extra ARGS are forwarded to ``colmena apply``.
    """
    _refresh_inventory()
    _check_no_concurrent_deploy()
    run_shell(["colmena", "apply", "--impure", "--on", f"@{stack}", *args],
              interactive=True, log_label=f"deploy-stack-{stack}")


@apply.command("tag")
@click.argument("tag")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def apply_tag(tag: str, args: tuple[str, ...]):
    """Deploy NixOS config to all hosts with a given tag.

    TAG is any Colmena deployment tag (e.g. ``core``, ``dns``).
    Functionally identical to ``apply stack`` but semantically for
    arbitrary tags rather than logical deployment layers.
    Extra ARGS are forwarded to ``colmena apply``.
    """
    _refresh_inventory()
    _check_no_concurrent_deploy()
    run_shell(["colmena", "apply", "--impure", "--on", f"@{tag}", *args],
              interactive=True, log_label=f"deploy-tag-{tag}")


# ── build ─────────────────────────────────────────────────────

@nixos.group("build")
def build():
    """Build NixOS closures without deploying.

    Evaluate and build system configurations locally. Useful for
    checking that configs compile before pushing to hosts.
    """


@build.command("host")
@click.argument("names", nargs=-1, required=True)
def build_host(names: tuple[str, ...]):
    """Build NixOS config for one or more hosts.

    NAMES are Colmena node names (e.g. ``netgate build auth``).
    """
    selector = ",".join(names)
    run_shell(["colmena", "build", "--impure", "--on", selector], interactive=True)


@build.command("stack")
@click.argument("stack")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def build_stack(stack: str, args: tuple[str, ...]):
    """Build NixOS config for all hosts in a logical stack.

    STACK is a Colmena deployment tag (e.g. ``platform``).
    Extra ARGS are forwarded to ``colmena build``.
    """
    run_shell(["colmena", "build", "--impure", "--on", f"@{stack}", *args], interactive=True)


@build.command("tag")
@click.argument("tag")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def build_tag(tag: str, args: tuple[str, ...]):
    """Build NixOS config for all hosts with a given tag.

    TAG is any Colmena deployment tag.
    Extra ARGS are forwarded to ``colmena build``.
    """
    run_shell(["colmena", "build", "--impure", "--on", f"@{tag}", *args], interactive=True)


# ── eval ──────────────────────────────────────────────────────

@nixos.group("eval")
def eval_group():
    """Inspect NixOS configuration without building.

    Evaluate Colmena node configurations and print results.
    Useful for checking option values, service config, etc.
    """


@eval_group.command("host")
@click.argument("name")
@click.argument("expr", required=False, default=None)
def eval_host(name: str, expr: str | None):
    """Evaluate NixOS config for a single host.

    NAME is the Colmena node name. EXPR is an optional Nix expression
    to evaluate within the node's config (e.g. ``config.services.nginx``).
    Without EXPR, prints the full system derivation path.
    """
    if expr:
        run_shell([
            "colmena", "eval", "--impure", "-E",
            f"{{ nodes, ... }}: nodes.{name}.config.{expr}",
        ], interactive=True)
    else:
        run_shell([
            "colmena", "eval", "--impure", "-E",
            f"{{ nodes, ... }}: nodes.{name}.config.system.build.toplevel",
        ], interactive=True)


@eval_group.command("stack")
@click.argument("stack")
@click.argument("expr", required=False, default=None)
def eval_stack(stack: str, expr: str | None):
    """Evaluate NixOS config for all hosts in a stack.

    STACK is a Colmena deployment tag. EXPR is an optional Nix
    expression to evaluate per host.
    """
    attr = expr or "system.build.toplevel"
    run_shell([
        "colmena", "eval", "--impure", "-E",
        f'{{ nodes, ... }}: builtins.mapAttrs (name: node: node.config.{attr}) '
        f'(lib.filterAttrs (_: n: builtins.elem "{stack}" '
        f'(n.config.deployment.tags or [])) nodes)',
    ], interactive=True)


@eval_group.command("tag")
@click.argument("tag")
@click.argument("expr", required=False, default=None)
def eval_tag(tag: str, expr: str | None):
    """Evaluate NixOS config for all hosts with a tag.

    TAG is any Colmena deployment tag. EXPR is an optional Nix
    expression to evaluate per host.
    """
    attr = expr or "system.build.toplevel"
    run_shell([
        "colmena", "eval", "--impure", "-E",
        f'{{ nodes, ... }}: builtins.mapAttrs (name: node: node.config.{attr}) '
        f'(lib.filterAttrs (_: n: builtins.elem "{tag}" '
        f'(n.config.deployment.tags or [])) nodes)',
    ], interactive=True)


# ── list ──────────────────────────────────────────────────────

@nixos.group("list")
def list_group():
    """Enumerate NixOS hosts and metadata.

    Query the Colmena hive and hosts.json for host names, tags,
    and deployment configuration.
    """


@list_group.command("hosts")
def list_hosts():
    """List all configured NixOS hosts from the Colmena hive.

    Runs ``colmena eval`` to enumerate all nodes in the hive
    and prints their names and target hosts.
    """
    result = subprocess.run(
        ["colmena", "eval", "--impure", "-E",
         "{ nodes, ... }: builtins.mapAttrs "
         "(name: node: node.config.deployment.targetHost or null) nodes"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Fallback to hosts.json
        root = find_project_root()
        hosts_file = fleet_cache_dir(root) / "hosts.json"
        if hosts_file.exists():
            with open(hosts_file) as f:
                hosts = json.load(f)
            table = Table(title="NixOS Hosts (from hosts.json)",
                          show_header=True, header_style="bold cyan")
            table.add_column("Name", style="green")
            table.add_column("IP")
            table.add_column("VMID", justify="right")
            table.add_column("Tags")

            for name, meta in sorted(hosts.items()):
                table.add_row(
                    name,
                    meta.get("ip", "") or "[dim]-[/dim]",
                    str(meta.get("vmid", "")),
                    ", ".join(meta.get("tags", [])) or "[dim]-[/dim]",
                )
            console.print(table)
        else:
            console.print("[red]ERROR:[/red] Could not eval hive or find hosts.json")
            sys.exit(1)
    else:
        click.echo(result.stdout)


@list_group.command("tags")
def list_tags():
    """List all tags/stacks currently in use across hosts.

    Reads ``.cache/fleet/hosts.json`` and aggregates all unique tags,
    showing which hosts belong to each.
    """
    root = find_project_root()
    hosts_file = fleet_cache_dir(root) / "hosts.json"

    if not hosts_file.exists():
        console.print("[red]ERROR:[/red] .cache/fleet/hosts.json not found")
        sys.exit(1)

    with open(hosts_file) as f:
        hosts = json.load(f)

    tag_map: dict[str, list[str]] = {}
    for name, meta in hosts.items():
        for tag in meta.get("tags", []):
            tag_map.setdefault(tag, []).append(name)

    if not tag_map:
        console.print("[dim]No tags found in hosts.json[/dim]")
        return

    table = Table(title="Host Tags", show_header=True, header_style="bold cyan")
    table.add_column("Tag", style="green")
    table.add_column("Count", justify="right")
    table.add_column("Hosts")

    for tag, members in sorted(tag_map.items()):
        table.add_row(tag, str(len(members)), ", ".join(sorted(members)))

    console.print(table)
