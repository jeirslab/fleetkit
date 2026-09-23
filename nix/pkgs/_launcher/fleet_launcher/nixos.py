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


def _first_reachable(candidates, port: int = 22, timeout: float = 3.0):
    """Return the first candidate address that accepts a TCP connection on
    ``port`` (SSH by default), or ``None`` if none answer. Used to choose a
    Colmena deploy target from a host's ordered ``deploy_ips`` candidates."""
    import socket
    for addr in candidates:
        if not addr:
            continue
        try:
            with socket.create_connection((addr, port), timeout=timeout):
                return addr
        except OSError:
            continue
    return None


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
@click.option("--parallel", type=int, default=None, metavar="N",
              help="With --wait: run at most N host jobs at once (default: all of "
                   "them). Bound it on a small runner — every job is a colmena "
                   "build+copy of its own.")
def apply_host(names: tuple[str, ...], ip: str | None, no_refresh: bool, no_session: bool,
               wait: bool, reboot: bool, dry_activate: bool, parallel: int | None):
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
        sys.exit(run_and_wait(specs, max_workers=parallel))

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
        # Patch hosts.json temporarily with override IP.
        # Do NOT re-import json here — the module already does (line 9), and a
        # function-local import binds the name for the WHOLE function body, so
        # the `elif` branch below (which never runs the import) died on an
        # UnboundLocalError. That branch is the default path of every
        # `fleet deploy nixos apply host` invoked without --ip.
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
    elif not ip:
        # First-reachable deploy target: a host that declares `deploy_ips`
        # (ordered fallback addresses beyond its primary `ip` — e.g. a Tailscale
        # address) gets each candidate probed on SSH, and Colmena is pointed at
        # the first that answers. Patched into hosts.json exactly like the
        # explicit --ip override, since colmena reads targetHost from there.
        # A host with no deploy_ips is untouched (the whole existing fleet);
        # an explicit --ip wins and skips probing.
        root = find_project_root()
        hosts_file = fleet_cache_dir(root) / "hosts.json"
        try:
            with open(hosts_file) as f:
                hosts = json.load(f)
        except (OSError, json.JSONDecodeError):
            hosts = {}
        patched = False
        for name in names:
            meta = hosts.get(name) or {}
            deploy_ips = meta.get("deploy_ips") or []
            if not deploy_ips:
                continue
            primary = meta.get("ip") or ""
            candidates: list[str] = []
            for c in [primary, *deploy_ips, meta.get("internal_ip") or ""]:
                if c and c not in candidates:
                    candidates.append(c)
            chosen = _first_reachable(candidates)
            if chosen is None:
                console.print(
                    f"[yellow]No reachable deploy address for {name}[/yellow] "
                    f"(tried {', '.join(candidates)}) — leaving {primary or 'unset'}"
                )
                continue
            if chosen != primary:
                meta["ip"] = chosen
                hosts[name] = meta
                patched = True
                console.print(
                    f"[green]Deploy target:[/green] {name} → {chosen} "
                    f"[dim](first reachable of {', '.join(candidates)})[/dim]"
                )
            else:
                console.print(f"[dim]Deploy target: {name} → {chosen} (primary reachable)[/dim]")
        if patched:
            with open(hosts_file, "w") as f:
                json.dump(hosts, f, indent=2)
                f.write("\n")
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



def _colmena_eval(root, expr: str) -> subprocess.CompletedProcess[str]:
    """``colmena eval`` of ``expr`` (a ``{ nodes, ... }:`` function) against the
    hive this checkout deploys — ``--impure`` like every other colmena call
    here, so hosts.json and secrets are readable during evaluation."""
    return subprocess.run(
        ["colmena", "eval", "--impure", "-E", expr],
        cwd=root, capture_output=True, text=True,
    )


def _hive_node_names(root) -> list[str]:
    """The hive's node names — the deployable set. Asked of colmena, not read
    from hosts.json: hosts.json also carries adopted (non-NixOS) guests, and a
    host with no closure is not a deploy target."""
    out = _colmena_eval(root, "{ nodes, ... }: builtins.attrNames nodes")
    if out.returncode != 0:
        console.print(f"[red]could not list the hive's nodes:[/red]\n{out.stderr.strip()}")
        sys.exit(1)
    return sorted(json.loads(out.stdout))


def _expected_system(root, name: str) -> tuple[str | None, str]:
    """The store path this tree would activate on ``name`` — evaluated, not
    built (an output path is a function of the derivation alone), and
    evaluated THROUGH COLMENA: the hive pins its own nixpkgs (overlays,
    allowUnfree, a path import that labels itself ``pre-git``), so a plain
    ``nixosConfigurations`` eval of the same modules is a different
    derivation and would never match what a host runs. Returns
    ``(path, error)``; ``path`` is None when the closure does not evaluate."""
    out = _colmena_eval(
        root, f'{{ nodes, ... }}: nodes."{name}".config.system.build.toplevel.outPath')
    if out.returncode != 0:
        tail = "\n".join(out.stderr.strip().splitlines()[-3:])
        return None, tail
    return json.loads(out.stdout.strip()), ""


def _expected_systems_bulk(root, names: list[str]) -> dict[str, tuple[str | None, str]]:
    """All of ``names`` in ONE colmena process: nixpkgs and the shared modules
    are evaluated once instead of once per node, which is most of the cost.
    Needs the memory for the whole hive (a few GB for ~25 nodes); one node
    that fails to evaluate fails the whole call, so skip such nodes."""
    sel = " ".join(f'"{n}" = null;' for n in names)
    out = _colmena_eval(
        root,
        "{ nodes, ... }: builtins.mapAttrs (_: n: n.config.system.build.toplevel.outPath) "
        f"(builtins.intersectAttrs {{ {sel} }} nodes)")
    if out.returncode != 0:
        tail = "\n".join(out.stderr.strip().splitlines()[-3:])
        return {n: (None, tail) for n in names}
    paths = json.loads(out.stdout)
    return {n: (paths[n], "") if n in paths else (None, "absent from the hive") for n in names}


def _system_label(path: str) -> str:
    """``nixos-system-<hostName>-<rest>`` from a system store path, or the
    basename when it is not shaped like one."""
    base = path.rsplit("/", 1)[-1]
    return base.split("-", 1)[1] if "-" in base else base


def _system_belongs_to(path: str, name: str) -> bool:
    """Whether a running system path was built for node ``name``: NixOS names
    the toplevel ``nixos-system-<networking.hostName>-<version>``, and the
    hive sets hostName to the node name. A mismatch means the address is
    answered by some other machine."""
    return _system_label(path).startswith(f"nixos-system-{name}-")


def _running_system(ip: str, *, timeout: int = 10) -> tuple[str | None, str]:
    """What ``ip`` is running now: the target of ``/run/current-system``.
    Returns ``(path, error)``; ``path`` is None when the host does not answer."""
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", f"ConnectTimeout={timeout}", f"root@{ip}",
         "readlink -f /run/current-system"],
        capture_output=True, text=True, timeout=timeout + 20,
    )
    if out.returncode != 0 or not out.stdout.strip():
        tail = "\n".join(out.stderr.strip().splitlines()[-2:]) or f"rc={out.returncode}"
        return None, tail
    return out.stdout.strip(), ""


@apply.command("changed")
@click.option("--skip", "skips", multiple=True, metavar="HOST",
              help="Leave HOST out (repeatable) — e.g. one whose closure does "
                   "not evaluate yet, or one that is deployed by hand.")
@click.option("--only", "onlys", multiple=True, metavar="HOST",
              help="Consider only HOST (repeatable). Default: every node of the hive.")
@click.option("--unreachable", type=click.Choice(["fail", "skip", "deploy"]),
              default="fail", show_default=True,
              help="A host that does not answer SSH: fail the run, leave it "
                   "out, or deploy it anyway (colmena will then fail on it).")
@click.option("--jobs", type=int, default=2, show_default=True, metavar="N",
              help="Concurrent host evaluations, one nix process each (two fit "
                   "an 8 GB runner). 0 = evaluate the whole hive in ONE process: "
                   "nixpkgs is evaluated once, not per node — far faster, needs "
                   "the memory for every node at once, and one node that does "
                   "not evaluate fails the lot (use --skip).")
@click.option("--parallel", type=int, default=4, show_default=True, metavar="N",
              help="Concurrent host deploys once the changed set is known.")
@click.option("--dry-run", is_flag=True,
              help="Report which hosts differ and exit without deploying.")
@click.option("--reboot", is_flag=True, help="Forwarded to `apply host`.")
@click.option("--dry-activate", "dry_activate", is_flag=True, help="Forwarded to `apply host`.")
@click.option("--build-on-target", is_flag=True,
              help="Build each closure on its host (`apply remote`) instead of "
                   "here — for a workstation without a remote builder. Not "
                   "combinable with --dry-activate.")
def apply_changed(skips: tuple[str, ...], onlys: tuple[str, ...], unreachable: str,
                  jobs: int, parallel: int, dry_run: bool, reboot: bool, dry_activate: bool,
                  build_on_target: bool):
    """Deploy every host whose running system differs from this tree.

    The continuous-deploy entry point: what a merge to the deploy branch
    runs. For each node of the hive the closure this checkout would
    activate is evaluated through colmena (not built) and compared with
    what the host reports at ``/run/current-system``. Hosts that already
    run it are left alone; the rest go through ``apply host … --wait`` and
    the exit code is the real aggregate result.

    Comparing against the hosts rather than against the previous commit
    means a host whose last deploy failed is picked up on the next push,
    and a deploy from a fresh clone needs no history. It costs one
    evaluation per host — the same work the gate does — and one SSH
    round-trip each.

    \b
    Examples:
      fleet deploy nixos apply changed --dry-run
      fleet deploy nixos apply changed --skip tracelab --unreachable skip
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ._util import fleet_executable

    root = find_project_root()
    _refresh_inventory()
    hosts_file = fleet_cache_dir(root) / "hosts.json"
    try:
        with open(hosts_file) as f:
            hosts = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]cannot read {hosts_file}:[/red] {exc}")
        sys.exit(1)

    names = _hive_node_names(root)
    if onlys:
        unknown = sorted(set(onlys) - set(names))
        if unknown:
            console.print(f"[red]not a node of the hive:[/red] {', '.join(unknown)}")
            sys.exit(1)
        names = [n for n in names if n in onlys]
    names = [n for n in names if n not in skips]
    if not names:
        console.print("[yellow]no hosts to consider[/yellow]")
        return

    expected: dict[str, tuple[str | None, str]] = {}
    if jobs <= 0:
        console.print(f"[dim]evaluating {len(names)} host closure(s) in one process…[/dim]")
        expected = _expected_systems_bulk(root, names)
        for name in names:
            path, _err = expected[name]
            console.print(f"  {name}: " + (f"[dim]{path.rsplit('/', 1)[-1]}[/dim]" if path
                                            else "[red]eval failed[/red]"))
    else:
        console.print(f"[dim]evaluating {len(names)} host closure(s), {jobs} at a time…[/dim]")
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(_expected_system, root, n): n for n in names}
            for fut in as_completed(futures):
                name = futures[fut]
                expected[name] = fut.result()
                path, _err = expected[name]
                # One line per host as it lands: a CI log shows progress, not silence.
                console.print(f"  {name}: " + (f"[dim]{path.rsplit('/', 1)[-1]}[/dim]" if path
                                                else "[red]eval failed[/red]"))

    def _probe(name: str) -> tuple[str | None, str]:
        entry = hosts.get(name) or {}
        ip = entry.get("ip") or entry.get("internal_ip") or ""
        if not ip:
            return None, "no ip in hosts.json"
        try:
            return _running_system(ip)
        except subprocess.TimeoutExpired:
            return None, "ssh timed out"

    console.print(f"[dim]asking {len(names)} host(s) what they run…[/dim]")
    with ThreadPoolExecutor(max_workers=8) as pool:
        running = dict(zip(names, pool.map(_probe, names)))

    changed: list[str] = []
    eval_failed: list[str] = []
    down: list[str] = []
    imposters: list[str] = []
    t = Table(title="deploy plan")
    t.add_column("host", style="cyan")
    t.add_column("state", style="bold")
    t.add_column("detail", overflow="fold")
    for name in names:
        want, eerr = expected[name]
        have, herr = running[name]
        if want is None:
            eval_failed.append(name)
            t.add_row(name, "[red]eval failed[/red]", eerr)
        elif have is None:
            down.append(name)
            t.add_row(name, "[yellow]unreachable[/yellow]", herr)
        elif not _system_belongs_to(have, name):
            # The address answered, but with another host's system: an IP
            # collision (two estates declaring the same address, a guest
            # re-provisioned under a new name). Deploying would overwrite a
            # machine that is not ours — never, whatever the flags say.
            imposters.append(name)
            t.add_row(name, "[red]wrong host[/red]",
                      f"runs {_system_label(have)} — not {name}; refusing")
        elif want == have:
            t.add_row(name, "[green]current[/green]", want.rsplit("/", 1)[-1])
        else:
            changed.append(name)
            t.add_row(name, "[magenta]changed[/magenta]",
                      f"{have.rsplit('/', 1)[-1]} → {want.rsplit('/', 1)[-1]}")
    console.print(t)

    rc = 0
    if imposters:
        console.print(f"[red]{len(imposters)} address(es) answer as another host:[/red] "
                      f"{', '.join(imposters)} — an IP collision; resolve it in the "
                      f"fleet declarations, nothing is deployed there")
        rc = 1
    if eval_failed:
        console.print(f"[red]{len(eval_failed)} host(s) do not evaluate:[/red] "
                      f"{', '.join(eval_failed)} — fix them or pass --skip")
        rc = 1
    if down:
        if unreachable == "fail":
            console.print(f"[red]{len(down)} host(s) unreachable:[/red] {', '.join(down)} "
                          f"(--unreachable skip|deploy to proceed without them)")
            rc = 1
        elif unreachable == "deploy":
            changed += down
        else:
            console.print(f"[yellow]leaving out unreachable:[/yellow] {', '.join(down)}")
    if rc:
        sys.exit(rc)
    if not changed:
        console.print("[green]every host already runs this tree — nothing to deploy[/green]")
        return
    console.print(f"[bold]deploying {len(changed)} host(s):[/bold] {', '.join(changed)}")
    if dry_run:
        return

    if build_on_target:
        if dry_activate:
            console.print("[red]--build-on-target cannot --dry-activate[/red]")
            sys.exit(2)
        cmd = [fleet_executable(), "deploy", "nixos", "apply", "remote", *changed]
        if reboot:
            cmd.append("--reboot")
    else:
        cmd = [fleet_executable(), "deploy", "nixos", "apply", "host", *changed,
               "--wait", "--no-refresh", "--parallel", str(parallel)]
        if reboot:
            cmd.append("--reboot")
        if dry_activate:
            cmd.append("--dry-activate")
    result = run_shell(cmd, cwd=root, interactive=True, exit_on_fail=False,
                       log_label="deploy-changed")
    sys.exit(result.returncode)


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
