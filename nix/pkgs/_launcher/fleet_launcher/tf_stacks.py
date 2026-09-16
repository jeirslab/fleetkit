"""fleet tf — Terranix/OpenTofu stack lifecycle.

Scope is `<env>.<stack>` dot-paths resolved by prefix-match against the
auto-generated leaf stack list. Examples:

    fleet deploy tf list
    fleet deploy tf apply platform.bootstrap
    fleet deploy tf apply platform                    # every platform.* leaf
    fleet deploy tf apply all                         # every leaf
    fleet deploy tf preview platform.core
    fleet deploy tf destroy dev.apps --target api-dev
    fleet deploy tf import platform.bootstrap proxmox_virtual_environment_pool.pool-core core

Every operation flows through `nix build .#tf-<env>-<stack-dashed>` to
regenerate config.tf.json from the unified fleet, then runs
`tofu -chdir=.tf/<env>-<stack-dashed>` in the repo-local workdir.

Destruction safety:
- `destroy` parses config.tf.json and refuses to run if any `--target`
  resolves to a block with `lifecycle.prevent_destroy = true`. The
  unprotect workflow is printed.
- `--target-dependents` doesn't exist in OpenTofu by design (-target is
  upward-only), so a destroy can never cascade downward into dependents.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ._util import find_project_root, run_shell

console = Console()


# ── On-disk cache (option 1+2: skip Nix when inputs unchanged) ───────
#
# Two layers of caching, both keyed by the same input fingerprint:
#
#   .tf/.cache/stack-ids.json   stack enumeration (replaces a 2-min eval)
#   .tf/<slug>/.cache.json      per-stack config.tf.json freshness marker
#
# Cache key = config.source_fingerprint — SHA256 over every tracked .nix
# plus flake.lock. Shared with the catalog cache in config.py on purpose:
# both caches answer "has the fleet's Nix source moved?", and when only one
# of them noticed a change the CLI acted on two different views of the same
# fleet. False positives (rebuilding when output would be identical) are
# cheap because Nix dedups in its store; false negatives are incorrect, so
# the fingerprint stays conservative.

def _input_hash(root: Path) -> str:
    from .config import source_fingerprint
    return source_fingerprint(root) or ""


def _cache_dir(root: Path) -> Path:
    d = root / ".tf" / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Stack discovery ──────────────────────────────────────────────────

def _leaf_stack_ids(root: Path) -> list[str]:
    """Enumerate every leaf stack — disk-cached.

    On cache hit (input fingerprint matches): returns cached IDs without
    invoking Nix at all.

    On miss: `nix build .#tf-stack-ids` reads the authoritative list from
    a tiny flake-output JSON file (eval-cached, ~ms). The previous code
    did `nix eval --impure --expr 'evalModules ...'` which bypassed the
    eval cache and took 2+ minutes.

    Returns IDs like "platform.bootstrap" (dots, not dashes).
    """
    cache = _cache_dir(root) / "stack-ids.json"
    key = _input_hash(root)
    if cache.exists():
        try:
            blob = json.loads(cache.read_text())
            if blob.get("key") == key and isinstance(blob.get("ids"), list):
                return blob["ids"]
        except (json.JSONDecodeError, OSError):
            pass
    try:
        out = subprocess.check_output(
            ["nix", "build", ".#tf-stack-ids", "--no-link", "--print-out-paths"],
            cwd=root, text=True,
        ).strip()
        ids = sorted(json.loads(Path(out).read_text()))
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]ERROR:[/red] could not enumerate tf stacks: {exc}")
        return []
    try:
        cache.write_text(json.dumps({"key": key, "ids": ids}))
    except OSError:
        pass
    return ids


def _slug(leaf_id: str) -> str:
    """platform.bootstrap → platform-bootstrap"""
    return leaf_id.replace(".", "-")


def _scope_matches(scope: str, leaf_id: str) -> bool:
    """True if `leaf_id` is covered by `scope` (exact or dotted-prefix)."""
    if scope in ("all", "*"):
        return True
    return leaf_id == scope or leaf_id.startswith(scope + ".")


def _resolve_scope(root: Path, scope: str) -> list[str]:
    """Return leaf IDs whose tree path starts with `scope`."""
    all_leaves = _leaf_stack_ids(root)
    hits = [l for l in all_leaves if _scope_matches(scope, l)]
    if not hits:
        console.print(f"[red]ERROR:[/red] No leaf stacks match scope '{scope}'.")
        console.print("Available leaves:")
        for l in all_leaves:
            console.print(f"  {l}")
        sys.exit(1)
    return hits


# ── Workdir management ───────────────────────────────────────────────

def _workdir(root: Path, leaf_id: str) -> Path:
    """.tf/<slug>/ — where tofu init + apply run."""
    return root / ".tf" / _slug(leaf_id)


def _stage_json(root: Path, leaf_id: str) -> Path:
    """Materialise .tf/<slug>/config.tf.json — skipping `nix build` when fresh.

    A sidecar `.cache.json` records the input fingerprint that produced
    the current `config.tf.json`. If the fingerprint still matches, we
    skip the Nix invocation entirely (a few seconds per call). Forcing a
    rebuild is `rm -rf .tf/<slug>` (or `rm .tf/<slug>/.cache.json`).
    """
    slug = _slug(leaf_id)
    workdir = _workdir(root, leaf_id)
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / "config.tf.json"
    sidecar = workdir / ".cache.json"
    key = _input_hash(root)

    if dest.exists() and sidecar.exists():
        try:
            if json.loads(sidecar.read_text()).get("key") == key:
                return workdir
        except (json.JSONDecodeError, OSError):
            pass

    out = subprocess.check_output(
        ["nix", "build", f".#tf-{slug}", "--no-link", "--print-out-paths"],
        cwd=root, text=True,
    ).strip()
    if not out:
        console.print(f"[red]ERROR:[/red] nix build .#tf-{slug} produced no output")
        sys.exit(1)
    # Unlink first: the source is a nix store path (mode 444) and any workdir
    # rendered before the chmod below existed still holds a 444 destination.
    # shutil.copy opens the destination for writing, so re-rendering into one
    # of those dies with EACCES and wedges the leaf permanently.
    dest.unlink(missing_ok=True)
    shutil.copy(out, dest)
    dest.chmod(0o644)
    try:
        sidecar.write_text(json.dumps({"key": key}))
    except OSError:
        pass
    return workdir


def _ensure_init(workdir: Path) -> None:
    """Run `tofu init` if `.terraform/` is absent."""
    if not (workdir / ".terraform").exists():
        subprocess.run(["tofu", "init"], cwd=workdir, check=True)


# ── State-address helpers (rekey) ────────────────────────────────────

def _state_addresses(workdir: Path) -> list[str]:
    """Every resource address in the leaf's tofu state (`tofu state list`)."""
    try:
        out = subprocess.check_output(["tofu", "state", "list"], cwd=workdir, text=True)
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]ERROR:[/red] `tofu state list` failed: {exc}")
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _name_segment(addr: str) -> str:
    """Resource-name component of a state address (the fleet key).

    `proxmox_virtual_environment_container.observe`       → `observe`
    `module.net.proxmox_x.observe["eth0"]`                → `observe`

    The emitter names resources after the fleet entry's attr key, so this
    segment is exactly what a host rename changes.
    """
    base = addr.split("[", 1)[0]
    return base.rsplit(".", 1)[-1]


def _rekey_address(addr: str, new_name: str) -> str:
    """Swap the resource-name segment of `addr` for `new_name`, keeping any
    `[index]` / `["key"]` suffix and any `module.` prefix intact."""
    base, sep, idx = addr.partition("[")
    prefix, dot, _name = base.rpartition(".")
    new_base = f"{prefix}{dot}{new_name}" if dot else new_name
    return new_base + (sep + idx if sep else "")


# ── Destruction safety preflight ─────────────────────────────────────

def _protected_targets(workdir: Path) -> set[str]:
    """Parse config.tf.json; return `{<type>.<name>}` addresses whose
    resource block carries `lifecycle.prevent_destroy = true`."""
    cfg_path = workdir / "config.tf.json"
    if not cfg_path.exists():
        return set()
    with open(cfg_path) as f:
        cfg = json.load(f)
    protected: set[str] = set()
    for typ, entries in (cfg.get("resource") or {}).items():
        for name, body in entries.items():
            lifecycle = body.get("lifecycle", [])
            if isinstance(lifecycle, list) and lifecycle and lifecycle[0].get("prevent_destroy"):
                protected.add(f"{typ}.{name}")
    return protected


def _raise_if_target_protected(workdir: Path, targets: list[str]) -> None:
    """Abort destroy if any target is protected."""
    if not targets:
        return
    protected = _protected_targets(workdir)
    hits = [t for t in targets if t in protected]
    if hits:
        console.print(f"[red]ERROR:[/red] Refusing to destroy protected resource(s): {hits}")
        console.print("")
        console.print("Unprotect workflow:")
        console.print("  1. Edit the fleet entry in nix/fleet/{fleet,resources}.nix: set protect = false")
        console.print("     (OR remove the stateful tag that forced it via destruction_policy)")
        console.print("  2. `fleet deploy tf apply <scope>` — lifecycle.prevent_destroy is removed")
        console.print("  3. `fleet deploy tf destroy <scope> --target <name>`")
        console.print("  4. Re-enable protect=true before the next apply")
        sys.exit(1)


# ── CLI ──────────────────────────────────────────────────────────────

@click.group("tf")
def tf_stacks() -> None:
    """Terranix/OpenTofu stack lifecycle.

    Scope is `<env>.<stack>` dot-paths. Prefix-match selects leaves.
    """
    pass


@tf_stacks.command("list")
def tf_list() -> None:
    """Enumerate leaf stacks."""
    root = find_project_root()
    leaves = _leaf_stack_ids(root)
    t = Table(title="Terranix leaf stacks")
    t.add_column("Stack ID", style="cyan")
    t.add_column("State prefix", style="dim")
    for l in leaves:
        t.add_row(l, f"tf/{_slug(l)}/")
    console.print(t)


@tf_stacks.command("preview")
@click.argument("scope")
@click.option("--target", multiple=True,
              help="Pass through to tofu -target. Can repeat. Limited to a single leaf.")
def tf_preview(scope: str, target: tuple[str, ...]) -> None:
    """tofu plan for leaves matching SCOPE."""
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if target and len(leaves) > 1:
        console.print(f"[red]ERROR:[/red] --target requires a single leaf (got {len(leaves)}).")
        sys.exit(1)
    for leaf in leaves:
        console.print(f"── preview {leaf} ──", style="bold cyan")
        wd = _stage_json(root, leaf)
        _ensure_init(wd)
        cmd = ["tofu", "plan"]
        for t in target:
            cmd += [f"-target={t}"]
        subprocess.run(cmd, cwd=wd, check=False)


@tf_stacks.command("apply")
@click.argument("scope")
@click.option("--target", multiple=True,
              help="Pass through to tofu -target. Can repeat. Limited to a single leaf.")
@click.option("--yes/--interactive", default=False,
              help="Pass -auto-approve to tofu.")
@click.option("--parallelism", default=3, type=int)
@click.option("--inventory/--no-inventory", default=True, show_default=True,
              help="After apply, refresh .cache/fleet/hosts.json from XOA (qemu-guest-agent "
                   "IP discovery for XCP-ng VMs). Only triggers on env=infra leaves; "
                   "proxmox applies skip the refresh regardless.")
def tf_apply(scope: str, target: tuple[str, ...], yes: bool, parallelism: int, inventory: bool) -> None:
    """tofu apply for leaves matching SCOPE.

    Post-apply inventory refresh policy:

      - env=infra (XCP-ng/XOA) leaves: refresh .cache/fleet/hosts.json so
        tier-0 VMs' DHCP-assigned IPs land in the inventory.
      - env=platform / env=dev (Proxmox) leaves: skipped. Proxmox CT
        IPs are declared statically in fleet; no discovery needed and
        the nix build cost isn't worth paying on every PVE apply.

    Pass --no-inventory to suppress even for XOA applies.
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if target and len(leaves) > 1:
        console.print(f"[red]ERROR:[/red] --target requires a single leaf (got {len(leaves)}).")
        sys.exit(1)

    # Track which applied leaves are XOA-affecting (env=infra).
    # Proxmox leaves don't trigger inventory refresh — Proxmox CT
    # IPs are static-declared, not DHCP-discovered.
    xoa_applied = False
    for leaf in leaves:
        console.print(f"── apply {leaf} ──", style="bold cyan")
        wd = _stage_json(root, leaf)
        _ensure_init(wd)
        cmd = ["tofu", "apply", f"-parallelism={parallelism}"]
        if yes:
            cmd.append("-auto-approve")
        for t in target:
            cmd += [f"-target={t}"]
        result = subprocess.run(cmd, cwd=wd, check=False)
        if result.returncode == 0 and leaf.startswith("infra."):
            xoa_applied = True

    if inventory and xoa_applied:
        console.print("[dim]── refreshing .cache/fleet/hosts.json from XOA ──[/dim]")
        try:
            from .inventory import generate_hosts_json
            generate_hosts_json(quiet=True)
            console.print("[green]✓[/green] inventory refreshed")
            console.print(
                "[dim]Tip: if a tier-0 VM's IP shows as empty, the guest agent "
                "may not have reported yet — re-run [bold]fleet inventory generate[/bold] "
                "in 30s.[/dim]"
            )
        except Exception as exc:
            console.print(f"[yellow]Warning:[/yellow] inventory refresh skipped: {exc}")


@tf_stacks.command("destroy")
@click.argument("scope")
@click.option("--target", multiple=True,
              help="Specific resource address to destroy. Preflight refuses if protected.")
@click.option("--yes/--interactive", default=False)
def tf_destroy(scope: str, target: tuple[str, ...], yes: bool) -> None:
    """tofu destroy for leaves matching SCOPE (preflight-protected)."""
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if target and len(leaves) > 1:
        console.print(f"[red]ERROR:[/red] --target requires a single leaf.")
        sys.exit(1)
    for leaf in leaves:
        wd = _stage_json(root, leaf)
        _raise_if_target_protected(wd, list(target))
        console.print(f"── destroy {leaf} ──", style="bold red")
        _ensure_init(wd)
        cmd = ["tofu", "destroy"]
        if yes:
            cmd.append("-auto-approve")
        for t in target:
            cmd += [f"-target={t}"]
        subprocess.run(cmd, cwd=wd, check=False)


@tf_stacks.command("import")
@click.argument("leaf")
@click.argument("address")
@click.argument("id_")
def tf_import(leaf: str, address: str, id_: str) -> None:
    """tofu import ADDRESS ID_ into the state for LEAF.

    Example:
      fleet deploy tf import platform.bootstrap \\
          proxmox_virtual_environment_pool.pool-core core
    """
    root = find_project_root()
    leaves = _resolve_scope(root, leaf)
    if len(leaves) != 1:
        console.print(f"[red]ERROR:[/red] import needs exactly one leaf, got: {leaves}")
        sys.exit(1)
    wd = _stage_json(root, leaves[0])
    _ensure_init(wd)
    subprocess.run(["tofu", "import", address, id_], cwd=wd, check=False)


@tf_stacks.command("refresh")
@click.argument("scope")
def tf_refresh(scope: str) -> None:
    """tofu refresh for matched leaves."""
    root = find_project_root()
    for leaf in _resolve_scope(root, scope):
        console.print(f"── refresh {leaf} ──", style="bold cyan")
        wd = _stage_json(root, leaf)
        _ensure_init(wd)
        subprocess.run(["tofu", "refresh"], cwd=wd, check=False)


@tf_stacks.command("init")
@click.argument("scope")
@click.option("--upgrade/--no-upgrade", default=False,
              help="Pass -upgrade to tofu init (refreshes provider lock).")
def tf_init(scope: str, upgrade: bool) -> None:
    """tofu init for matched leaves.

    Use --upgrade to refresh .terraform.lock.hcl after the provider set
    in nix/terranix/providers/ changes (e.g. when a new provider is
    added to the shared providers/default.nix). Plain `init` is normally
    handled implicitly by preview/apply/destroy/refresh/import — only
    invoke this directly when you need lock-file maintenance.
    """
    root = find_project_root()
    for leaf in _resolve_scope(root, scope):
        console.print(f"── init {leaf}{' (upgrade)' if upgrade else ''} ──", style="bold cyan")
        wd = _stage_json(root, leaf)
        cmd = ["tofu", "init"]
        if upgrade:
            cmd.append("-upgrade")
        subprocess.run(cmd, cwd=wd, check=False)


# ── Backend migration ────────────────────────────────────────────────
#
# Which backend a stack uses is decided in Nix (fleet.settings.backend plus
# any perStack override) and lands in config.tf.json. Changing the setting
# therefore changes where tofu LOOKS, and does nothing whatsoever about the
# state already sitting in the old backend. Left alone, the next plan reads
# an empty backend as "none of this exists yet" and the next apply rebuilds
# the fleet — which is why this is a verb with verification rather than a
# note telling operators to run `tofu init -migrate-state` themselves.
#
# The ordering below is the whole trick: `.terraform/` still binds the OLD
# backend until init re-binds it, so the pre-migration `state pull` must
# happen BEFORE the new config.tf.json is staged.

def _backend_of(workdir: Path) -> tuple[str, dict]:
    """The backend kind and block recorded in a staged config.tf.json."""
    cfg_path = workdir / "config.tf.json"
    try:
        cfg = json.loads(cfg_path.read_text())
    except (json.JSONDecodeError, OSError):
        return "", {}
    backend = (cfg.get("terraform") or {}).get("backend") or {}
    if not backend:
        return "", {}
    kind = next(iter(backend))
    return kind, backend[kind] or {}


def _strip_nulls(value):
    """Drop null-valued keys, recursively. Tofu records unset backend fields
    (`endpoints.dynamodb`, `sts`, …) as explicit nulls; writing those back
    into a backend block is not the same document it started with."""
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    return value


def _initialised_backend(workdir: Path) -> tuple[str, dict]:
    """The backend this workdir is actually BOUND to, per `.terraform/`.

    config.tf.json says where the fleet now wants state to live; this says
    where it currently lives. The two diverge the moment anything re-renders
    the config — including a previous `migrate-backend --dry-run` — and when
    they do, this is the only remaining record of the old backend.
    """
    try:
        st = json.loads((workdir / ".terraform" / "terraform.tfstate").read_text())
    except (OSError, json.JSONDecodeError):
        return "", {}
    backend = st.get("backend") or {}
    return backend.get("type") or "", _strip_nulls(backend.get("config") or {})


def _pin_config_to_initialised(workdir: Path) -> bool:
    """Put config.tf.json's backend block back to the initialised one.

    `tofu state pull` refuses outright when the declared backend differs from
    the initialised one — "Backend type changed from s3 to pg" — so a workdir
    whose config was already re-rendered cannot be read AT ALL until this is
    undone. Without it a single --dry-run permanently strands the state it
    was meant to describe. Returns True if anything changed.
    """
    kind, cfg = _initialised_backend(workdir)
    if not kind:
        return False
    path = workdir / "config.tf.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    tf = doc.setdefault("terraform", {})
    if tf.get("backend") == {kind: cfg}:
        return False
    tf["backend"] = {kind: cfg}
    path.unlink(missing_ok=True)          # staged copies are mode 444
    path.write_text(json.dumps(doc, indent=2))
    path.chmod(0o644)
    # config.tf.json is now something `nix build` did not produce, so the
    # fingerprint sidecar no longer describes it. Leaving it would let the
    # next _stage_json take a cache hit and keep this hand-written backend.
    (workdir / ".cache.json").unlink(missing_ok=True)
    return True


def _resource_index(state: dict) -> dict[str, str]:
    """Every resource instance in a state, as {address: canonical attributes}.

    This is the inventory itself rather than a marker standing in for it, so
    comparing two of these answers "did the same resources arrive, unchanged"
    directly. Attributes are canonicalised with sorted keys so the comparison
    survives a backend that reserialises the JSON on write.
    """
    index: dict[str, str] = {}
    for res in state.get("resources") or []:
        addr = ".".join(part for part in (
            res.get("module"),
            "data" if res.get("mode") == "data" else None,
            res.get("type"),
            res.get("name"),
        ) if part)
        for pos, inst in enumerate(res.get("instances") or []):
            key = inst.get("index_key", pos)
            # A deposed instance is a distinct object at the same address;
            # collapsing it into the live one would hide a lost leftover.
            suffix = f" deposed={inst['deposed']}" if inst.get("deposed") else ""
            index[f"{addr}[{key}]{suffix}"] = json.dumps(inst.get("attributes"),
                                                         sort_keys=True)
    return index


def _state_identity(workdir: Path) -> tuple[dict | None, str]:
    """Identity of the state in the CURRENTLY initialised backend.

    The `index` — every resource instance address mapped to its attributes —
    is what actually answers the question a migration has to answer: is what
    landed the same inventory, or a different (possibly empty) one wearing
    the same name. lineage and serial are recorded alongside it for reporting,
    but they are backend bookkeeping, not evidence: see the migrate-backend
    verification for why neither survives a legitimate cross-backend move.

    Returns (identity, reason). A None identity means the state could not be
    read at all — an unreachable backend or a workdir that was never
    initialised. That is deliberately not treated as "empty": an unreachable
    old backend looks identical to an empty one, and migrating on that
    assumption is how a fleet loses its inventory.

    `reason` carries tofu's own stderr. Reporting "could not read state" and
    nothing else sends you looking at the backend when the actual cause is
    usually a missing credential in the environment — the two are
    indistinguishable from the message alone.
    """
    r = subprocess.run(["tofu", "state", "pull"], cwd=workdir,
                       capture_output=True, text=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        err = " ".join((r.stderr or "").split())
        return None, (err[:300] or f"`tofu state pull` exited {r.returncode} with no output")
    try:
        st = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, "the backend returned something that is not valid state JSON"
    resources = st.get("resources") or []
    index = _resource_index(st)
    return {
        "lineage": st.get("lineage"),
        "serial": st.get("serial"),
        "resources": len(resources),
        "instances": sum(len(res.get("instances") or []) for res in resources),
        "index": index,
        "raw": r.stdout,
    }, ""


def _fmt_backend(kind: str, cfg: dict) -> str:
    """One-line rendering of a backend block, for the plan table."""
    if kind == "s3":
        return f"s3 {cfg.get('bucket')}/{cfg.get('key')}"
    if kind == "pg":
        return f"pg schema={cfg.get('schema_name')}"
    if kind == "local":
        return f"local {cfg.get('path')}"
    return kind or "(none)"


@tf_stacks.command("migrate-backend")
@click.argument("scope")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--dry-run", is_flag=True,
              help="Report the old → new backend per leaf and stop, moving no state.")
@click.option("--backup-dir", default=".tf/.migrate-backup", show_default=True,
              type=click.Path(file_okay=False),
              help="Where the pre-migration state dump is written.")
def tf_migrate_backend(scope: str, yes: bool, dry_run: bool, backup_dir: str) -> None:
    """Move matched leaves' state into the backend the fleet NOW declares.

    Run this AFTER changing fleet.settings.backend (or a perStack override)
    and BEFORE the next plan or apply. For each leaf it:

    \b
      1. reads the full resource inventory from the old backend
      2. dumps that state to --backup-dir (refusing to clobber an existing dump)
      3. re-renders config.tf.json so the new backend block is in place
      4. runs `tofu init -migrate-state`, copying the state across
      5. re-reads the inventory from the new backend and REFUSES to call it a
         success unless every resource instance arrived with byte-identical
         attributes — nothing missing, nothing extra, nothing altered

    lineage and serial are reported but deliberately not enforced: a
    destination backend mints its own state on first write (pg re-stamps
    lineage and resets serial to 1), so requiring them to survive would fail
    every genuine cross-backend migration.

    A leaf whose old state cannot be read is skipped, not migrated: an
    unreachable backend and an empty one are indistinguishable from here, and
    only one of them is safe to proceed from.

    \b
    Example — after switching the fleet from S3 to Postgres:
      fleet deploy tf migrate-backend all --dry-run
      fleet deploy tf migrate-backend all
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)

    # ── plan: what each leaf moves from and to ─────────────────────
    plans: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for leaf in leaves:
        wd = _workdir(root, leaf)
        if not (wd / "config.tf.json").exists() or not (wd / ".terraform").exists():
            skipped.append((leaf, "never initialised — no state to move; "
                                  "`fleet deploy tf init` binds it to the new backend directly"))
            continue
        # Trust `.terraform`, not config.tf.json: an earlier run of this very
        # command (or any verb that re-renders) may already have replaced the
        # declared backend with the target one, and reading THAT would report
        # the migration as already done while the state sits in the old
        # backend, unreferenced.
        _pin_config_to_initialised(wd)
        old_kind, old_cfg = _initialised_backend(wd)
        if not old_kind:
            old_kind, old_cfg = _backend_of(wd)
        before, why = _state_identity(wd)
        if before is None:
            skipped.append((leaf, f"could not read state from the current backend "
                                  f"({old_kind or 'unknown'}): {why}"))
            continue
        # Re-render against the current settings. The sidecar is dropped first
        # because a cache hit here would compare the old backend with itself
        # and report nothing to do.
        (wd / ".cache.json").unlink(missing_ok=True)
        _stage_json(root, leaf)
        new_kind, new_cfg = _backend_of(wd)
        # A dry run must leave the workdir exactly as it found it. Re-rendering
        # is how we learn the target backend, so undo it here rather than skip
        # it — otherwise `--dry-run`, the cautious option, is the one that
        # strands the state.
        if dry_run:
            _pin_config_to_initialised(wd)
        if (new_kind, new_cfg) == (old_kind, old_cfg):
            skipped.append((leaf, f"already on {_fmt_backend(old_kind, old_cfg)}"))
            continue
        plans.append({"leaf": leaf, "wd": wd, "before": before,
                      "old": (old_kind, old_cfg), "new": (new_kind, new_cfg)})

    for leaf, why in skipped:
        console.print(f"[dim]skip[/dim]     {leaf}: {why}")
    if not plans:
        console.print("[green]nothing to migrate[/green]")
        return

    t = Table(title="Backend migration plan")
    t.add_column("Stack", style="cyan")
    t.add_column("From", style="yellow")
    t.add_column("To", style="green")
    t.add_column("Instances", justify="right")
    t.add_column("Serial", justify="right", style="dim")
    for p in plans:
        t.add_row(p["leaf"], _fmt_backend(*p["old"]), _fmt_backend(*p["new"]),
                  str(p["before"]["instances"]), str(p["before"]["serial"]))
    console.print(t)

    # The launcher exports PG_CONN_STR from SOPS; when it could not, tofu
    # fails inside init with a message about the backend rather than about the
    # missing secret, after the workdir has already been re-rendered.
    if any(p["new"][0] == "pg" for p in plans) and not os.environ.get("PG_CONN_STR"):
        console.print("[red]ERROR:[/red] target backend is pg but PG_CONN_STR is unset.")
        console.print("         Set fleet.settings.backend.pg.connStrSopsPath to the SOPS path "
                      "holding the libpq connection string.")
        sys.exit(1)

    if dry_run:
        console.print("[dim]--dry-run: no state moved.[/dim]")
        return
    if not yes:
        click.confirm(f"Migrate {len(plans)} stack(s)?", abort=True)

    # ── migrate ────────────────────────────────────────────────────
    backups = Path(backup_dir)
    backups.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for p in plans:
        leaf, wd, before = p["leaf"], p["wd"], p["before"]
        console.print(f"── migrate-backend {leaf}: "
                      f"{_fmt_backend(*p['old'])} → {_fmt_backend(*p['new'])} ──",
                      style="bold cyan")

        dump = backups / f"{_slug(leaf)}.pre-migrate.tfstate"
        if dump.exists():
            # Re-running would overwrite the pre-migration dump with a
            # post-migration one, destroying the only copy of the thing the
            # backup exists to protect.
            console.print(f"[red]FAIL[/red]     {dump} already exists — move it aside first.")
            failures.append(leaf)
            continue
        dump.write_text(before["raw"])
        console.print(f"[dim]         backed up {before['instances']} instance(s) → {dump}[/dim]")

        # -force-copy answers tofu's own copy prompt: we already confirmed
        # above, and -input=false turns any further prompt into an error
        # rather than a hang in a non-interactive run.
        r = subprocess.run(["tofu", "init", "-migrate-state", "-force-copy", "-input=false"],
                           cwd=wd, check=False)
        if r.returncode != 0:
            console.print(f"[red]FAIL[/red]     `tofu init -migrate-state` exited {r.returncode}. "
                          f"State is unchanged in the old backend; the dump is at {dump}.")
            failures.append(leaf)
            continue

        after, why = _state_identity(wd)
        if after is None:
            console.print(f"[red]FAIL[/red]     init succeeded but the new backend returns no state "
                          f"({why}). "
                          f"Do NOT apply. Restore with: fleet deploy tf state-push {leaf} {dump}")
            failures.append(leaf)
            continue

        # Compare the inventory, not tofu's bookkeeping. A destination backend
        # mints its own state on first write: pg re-stamps `lineage` and resets
        # `serial` to 1, and both are correct behaviour for a real migration.
        # Failing on either reports a clean move as a disaster — and, worse,
        # trains the operator to ignore the one check that would catch a real
        # one. What must not change is which resources are tracked and what
        # they say, so that is what is checked.
        old_index, new_index = before["index"], after["index"]
        missing = sorted(set(old_index) - set(new_index))
        extra = sorted(set(new_index) - set(old_index))
        altered = sorted(a for a in set(old_index) & set(new_index)
                         if old_index[a] != new_index[a])
        if missing or extra or altered:
            console.print("[red]FAIL[/red]     migrated state does not match the original:")
            for addr in missing[:10]:
                console.print(f"         - missing from the new backend: {addr}")
            for addr in extra[:10]:
                console.print(f"         - present only in the new backend: {addr}")
            for addr in altered[:10]:
                console.print(f"         - attributes changed: {addr}")
            dropped = len(missing) + len(extra) + len(altered) - min(len(missing), 10) \
                - min(len(extra), 10) - min(len(altered), 10)
            if dropped > 0:
                console.print(f"         - … and {dropped} more")
            console.print(f"         Do NOT apply. Restore with: "
                          f"fleet deploy tf state-push {leaf} {dump}")
            failures.append(leaf)
            continue

        restamped = ""
        if after["lineage"] != before["lineage"]:
            restamped = (f", new lineage {after['lineage'][:8]} "
                         f"(expected: {p['new'][0]} minted its own)")
        console.print(f"[green]OK[/green]       {after['instances']} instance(s) verified "
                      f"identical, serial {before['serial']} → {after['serial']}{restamped}")

    done = len(plans) - len(failures)
    console.print(f"[bold]migrated {done}/{len(plans)}[/bold]"
                  + (f" — failed: {', '.join(failures)}" if failures else ""))
    if failures:
        sys.exit(1)


@tf_stacks.command("state-untaint")
@click.argument("scope")
@click.argument("addr")
def tf_state_untaint(scope: str, addr: str) -> None:
    """tofu untaint ADDR for the single leaf SCOPE.

    Clears the tainted flag on a resource so the next plan doesn't
    force replacement. Useful after a provider-side hiccup left a
    resource in a half-applied state (e.g. provider returned
    unexpected attribute on create).
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if len(leaves) != 1:
        console.print(f"[red]ERROR:[/red] state-untaint requires exactly one leaf (got {len(leaves)}).")
        sys.exit(1)
    leaf = leaves[0]
    console.print(f"── state-untaint {leaf}: {addr} ──", style="bold cyan")
    wd = _stage_json(root, leaf)
    _ensure_init(wd)
    subprocess.run(["tofu", "untaint", addr], cwd=wd, check=False)


@tf_stacks.command("state-rm")
@click.argument("scope")
@click.argument("addr")
def tf_state_rm(scope: str, addr: str) -> None:
    """tofu state rm ADDR for the single leaf SCOPE.

    Removes a resource from tofu state without touching the real
    infrastructure. Useful when a provider renames a resource type and
    `state mv` refuses (it requires same type both sides); pair this
    with `tf apply` to have the new resource type take ownership of
    the existing infra.
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if len(leaves) != 1:
        console.print(f"[red]ERROR:[/red] state-rm requires exactly one leaf (got {len(leaves)}).")
        sys.exit(1)
    leaf = leaves[0]
    console.print(f"── state-rm {leaf}: {addr} ──", style="bold cyan")
    wd = _stage_json(root, leaf)
    _ensure_init(wd)
    subprocess.run(["tofu", "state", "rm", addr], cwd=wd, check=False)


@tf_stacks.command("state-mv")
@click.argument("scope")
@click.argument("from_addr")
@click.argument("to_addr")
def tf_state_mv(scope: str, from_addr: str, to_addr: str) -> None:
    """tofu state mv FROM_ADDR TO_ADDR for the single leaf SCOPE.

    Migrates an existing resource to a new address without destroy +
    recreate. Useful when a provider renames a resource type (e.g.
    proxmox_virtual_environment_cluster_options →
    proxmox_cluster_options in bpg/proxmox).

    Example:
      fleet deploy tf state-mv platform.bootstrap \\
        proxmox_virtual_environment_cluster_options.foo \\
        proxmox_cluster_options.foo
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if len(leaves) != 1:
        console.print(f"[red]ERROR:[/red] state-mv requires exactly one leaf (got {len(leaves)}).")
        sys.exit(1)
    leaf = leaves[0]
    console.print(f"── state-mv {leaf}: {from_addr} → {to_addr} ──", style="bold cyan")
    wd = _stage_json(root, leaf)
    _ensure_init(wd)
    subprocess.run(["tofu", "state", "mv", from_addr, to_addr], cwd=wd, check=False)


@tf_stacks.command("rekey")
@click.argument("scope")
@click.argument("old_name")
@click.argument("new_name")
@click.option("--dry-run", is_flag=True,
              help="Print the `state mv` commands without running them.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def tf_rekey(scope: str, old_name: str, new_name: str, dry_run: bool, yes: bool) -> None:
    """Rename an instantiated resource's STATE key: OLD_NAME → NEW_NAME.

    Renaming a host in the fleet makes OpenTofu see the renamed entry as
    "destroy old + create new" — catastrophic for a stateful container.
    This moves every state address whose resource-name segment is exactly
    OLD_NAME (the container plus any sibling resources the emitter keys
    off the same fleet name) to NEW_NAME, so the next `apply` is an
    in-place update instead of a replace. It codifies the manual
    `tofu state mv`-before-apply dance a host rename otherwise requires.

    This is only the STATE half of a rename. Full workflow:

      1. Rename the fleet entry yourself: the attr key in
         nix/hosts/<provider>/<old>.nix, the hostsRegistry key, and the
         file → <new>.nix.
      2. fleet deploy tf rekey <stack> <old> <new>     ← you are here
      3. fleet deploy tf apply <stack> --target <type>.<new>
         (the PVE guest name/hostname updates in place)

    Touches tofu state only — never running infra, and never the Nix
    files (do step 1 first). The trailing plan flags a destroy/replace if
    the rename was incomplete (a sibling wasn't moved) or an attr drifted.

    Example:
      fleet deploy tf rekey platform.core oldname newname
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if len(leaves) != 1:
        console.print(f"[red]ERROR:[/red] rekey requires exactly one leaf (got {len(leaves)}).")
        sys.exit(1)
    leaf = leaves[0]
    if old_name == new_name:
        console.print("[red]ERROR:[/red] OLD_NAME and NEW_NAME are identical — nothing to do.")
        sys.exit(1)

    wd = _stage_json(root, leaf)
    _ensure_init(wd)

    addresses = _state_addresses(wd)
    matches = [a for a in addresses if _name_segment(a) == old_name]
    if not matches:
        console.print(f"[red]ERROR:[/red] No state addresses with resource name "
                      f"'{old_name}' in {leaf}.")
        near = [a for a in addresses if old_name in a]
        if near:
            console.print("[yellow]Near matches[/yellow] (name segment must match exactly):")
            for a in near:
                console.print(f"  {a}")
        sys.exit(1)

    # Collision guard: refuse if NEW_NAME already owns state here — the
    # apply would have conflicted too.
    collisions = [a for a in addresses if _name_segment(a) == new_name]
    if collisions:
        console.print(f"[red]ERROR:[/red] '{new_name}' already exists in {leaf} state: "
                      f"{collisions}")
        console.print("Resolve the collision before rekeying.")
        sys.exit(1)

    moves = [(a, _rekey_address(a, new_name)) for a in matches]

    console.print(f"── rekey {leaf}: {old_name} → {new_name} ──", style="bold cyan")
    console.print(f"[dim]{len(moves)} state address(es) to move:[/dim]")
    for src, dst in moves:
        console.print(f"  {src}  →  {dst}")

    # Report-only heads-up: addresses that merely contain the old name but
    # whose name segment differs (e.g. a DNS record `dns-observe`). We do
    # NOT auto-move these — that would risk false matches — but flag them
    # so the operator can `state-mv` them by hand if they belong here.
    untouched = [a for a in addresses
                 if old_name in a and _name_segment(a) != old_name]
    if untouched:
        console.print("")
        console.print("[yellow]Note:[/yellow] contain the old name but the name segment "
                      "doesn't match exactly — NOT moved:")
        for a in untouched:
            console.print(f"  {a}")
        console.print("[dim]If they belong to this host, move them with "
                      "`fleet deploy tf state-mv` manually.[/dim]")

    if dry_run:
        console.print("")
        console.print("[dim]--dry-run: equivalent commands ──[/dim]")
        for src, dst in moves:
            console.print(f"  tofu -chdir=.tf/{_slug(leaf)} state mv '{src}' '{dst}'")
        return

    if not yes and not click.confirm(f"Move {len(moves)} state address(es)?", default=False):
        console.print("Aborted.")
        return

    for src, dst in moves:
        console.print(f"  mv {src} → {dst}", style="dim")
        res = subprocess.run(["tofu", "state", "mv", src, dst], cwd=wd, check=False)
        if res.returncode != 0:
            console.print(f"[red]ERROR:[/red] state mv failed for {src}; stopping. State is "
                          "partially migrated — finish the remaining moves manually.")
            sys.exit(1)

    console.print("[green]✓[/green] state rekeyed. Verifying with a targeted plan…")
    new_addr_types = sorted({dst.split("[", 1)[0] for _, dst in moves})
    cmd = ["tofu", "plan"]
    for a in new_addr_types:
        cmd += [f"-target={a}"]
    subprocess.run(cmd, cwd=wd, check=False)
    console.print("")
    console.print("[dim]If the plan above shows a destroy/replace, the rename was incomplete "
                  "(a sibling wasn't moved) or an attribute drifted. Otherwise finish with "
                  f"[bold]fleet deploy tf apply {leaf} --target {new_addr_types[0]}[/bold] to land "
                  "the in-place hostname update.[/dim]")

@tf_stacks.command("state-pull")
@click.argument("scope")
@click.argument("out", type=click.Path(dir_okay=False))
def tf_state_pull(scope: str, out: str) -> None:
    """tofu state pull for the single leaf SCOPE, written to OUT.

    Dumps the remote state as JSON for offline inspection or surgical
    edits (e.g. rewriting a stale node_name that blocks refresh). Pair
    with `state-push` after editing; remember to increment the
    top-level `serial` field or the push is rejected.
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if len(leaves) != 1:
        console.print(f"[red]ERROR:[/red] state-pull requires exactly one leaf (got {len(leaves)}).")
        sys.exit(1)
    leaf = leaves[0]
    console.print(f"── state-pull {leaf} → {out} ──", style="bold cyan")
    wd = _stage_json(root, leaf)
    _ensure_init(wd)
    with open(out, "w") as f:
        subprocess.run(["tofu", "state", "pull"], cwd=wd, check=True, stdout=f)


@tf_stacks.command("state-push")
@click.argument("scope")
@click.argument("statefile", type=click.Path(exists=True, dir_okay=False))
def tf_state_push(scope: str, statefile: str) -> None:
    """tofu state push STATEFILE for the single leaf SCOPE.

    Uploads a locally edited state. tofu refuses lineage mismatches and
    stale serials by default (no -force here on purpose) — pull with
    `state-pull`, edit, bump `serial`, then push. Back the original up
    first; state surgery has no undo beyond your copy.
    """
    root = find_project_root()
    leaves = _resolve_scope(root, scope)
    if len(leaves) != 1:
        console.print(f"[red]ERROR:[/red] state-push requires exactly one leaf (got {len(leaves)}).")
        sys.exit(1)
    leaf = leaves[0]
    console.print(f"── state-push {leaf} ← {statefile} ──", style="bold cyan")
    wd = _stage_json(root, leaf)
    _ensure_init(wd)
    subprocess.run(["tofu", "state", "push", statefile], cwd=wd, check=True)


@tf_stacks.command("state-export")
@click.argument("scope", default="all")
@click.option("--out-dir", default="state-export", show_default=True,
              type=click.Path(file_okay=False),
              help="Directory to write <leaf>.tfstate.json files into.")
def tf_state_export(scope: str, out_dir: str) -> None:
    """Export live tfstate JSON for SCOPE (or the whole fleet) to files.

    SCOPE is a leaf id or prefix (default "all" = every leaf stack). One
    <env>-<stack>.tfstate.json per leaf lands in --out-dir, plus a
    manifest.json listing what was exported. Read-only: this is
    `tofu state pull` per stack, nothing is modified.
    """
    import json as _json

    root = find_project_root()
    leaves = _resolve_scope(root, scope or "all")
    if not leaves:
        console.print("[red]ERROR:[/red] no leaf stacks matched.")
        sys.exit(1)
    outp = Path(out_dir)
    outp.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for leaf in leaves:
        slug = leaf.replace(".", "-")
        dest = outp / f"{slug}.tfstate.json"
        console.print(f"── state-export {leaf} → {dest} ──", style="bold cyan")
        wd = _stage_json(root, leaf)
        _ensure_init(wd)
        with open(dest, "w") as f:
            rc = subprocess.run(["tofu", "state", "pull"], cwd=wd, check=False, stdout=f)
        ok = rc.returncode == 0 and dest.stat().st_size > 0
        manifest[leaf] = {"file": dest.name, "ok": ok}
        if not ok:
            console.print(f"[yellow]WARN:[/yellow] export failed for {leaf}")
    (outp / "manifest.json").write_text(_json.dumps(manifest, indent=2))
    good = sum(1 for m in manifest.values() if m["ok"])
    console.print(f"exported {good}/{len(manifest)} stacks → {outp}/", style="bold green")


__all__ = ["tf_stacks"]


def _backend_check_pg(root: Path, backend: dict) -> None:
    """backend-check for the pg backend: connection string, reachability, rights.

    The pg backend fails in its own ways, none of which look like the S3 ones.
    PG_CONN_STR is resolved by the launcher bootstrap and swallowed on error,
    so an unset variable reaches tofu as a backend error with no mention of
    SOPS; and a role that can connect but cannot CREATE produces a permission
    error only at the moment the FIRST stack tries to make its schema, long
    after the backend looked fine.
    """
    conn = os.environ.get("PG_CONN_STR")
    path = (_cfg_get_lazy("backend_pg.conn_str_sops_path") or "")
    if not conn:
        console.print("[red]FAIL[/red]     PG_CONN_STR is unset.")
        if not path:
            console.print("[yellow]  →[/yellow]      fleet.settings.backend.pg.connStrSopsPath "
                          "is not set, so the launcher had nothing to resolve.")
        else:
            console.print(f"[yellow]  →[/yellow]      the launcher tried {path} and got nothing — "
                          f"check that key exists and that the SOPS file holding its "
                          f"top-level tree is decryptable.")
        console.print("[red]VERDICT[/red]  backend NOT healthy — no connection string")
        sys.exit(1)
    # Never print the string itself: it carries the password.
    console.print(f"[green]creds[/green]    PG_CONN_STR resolved"
                  + (f" from {path}" if path else " from the environment"))

    psql = shutil.which("psql")
    # `nix run` runs the package's DEFAULT program, so `nixpkgs#postgresql --
    # psql` execs bin/postgresql — which does not exist (the server binary is
    # bin/postgres) — and passes "psql" to it as an argument. The awscli2
    # fallback below gets away with `nix run` because there the default
    # program IS the one we want; here it has to be `nix shell --command`.
    prefix = [psql] if psql else [
        "nix", "shell", "--inputs-from", str(root), "nixpkgs#postgresql",
        "--command", "psql"]
    q = ("select current_user, current_database(), "
         "has_database_privilege(current_user, current_database(), 'CREATE')")
    r = subprocess.run(prefix + [conn, "-At", "-F", "|", "-c", q],
                       capture_output=True, text=True, timeout=90, check=False)
    if r.returncode != 0:
        last = (r.stderr or "").strip().splitlines()[-1:] or [""]
        console.print(f"[red]FAIL[/red]     cannot connect: {last[0]}")
        console.print("[red]VERDICT[/red]  backend NOT healthy — see above")
        sys.exit(1)
    user, db, can_create = (r.stdout.strip().split("|") + ["", "", ""])[:3]
    console.print(f"[green]connect[/green]  {user}@{db}")

    ok = True
    if can_create != "t":
        console.print(f"[red]FAIL[/red]     {user} lacks CREATE on {db} — the backend makes one "
                      f"schema per stack and cannot.")
        ok = False

    # mkFleet flattens backend.pg.schemaPrefix into the catalog as
    # `pgSchemaPrefix` (see backend' in flake.nix), so read it under that name.
    prefix_name = backend.get("pgSchemaPrefix") or "tf_"
    r = subprocess.run(
        prefix + [conn, "-At", "-c",
                  "select schema_name from information_schema.schemata "
                  f"where schema_name like '{prefix_name}%' order by 1"],
        capture_output=True, text=True, timeout=90, check=False)
    schemas = [s for s in (r.stdout or "").split() if s]
    console.print(f"[green]schemas[/green]  {len(schemas)} stack schema(s) under '{prefix_name}'"
                  + (f": {', '.join(schemas)}" if schemas else
                     " — none yet, expected before the first apply or migration"))

    console.print("[green]VERDICT[/green]  backend healthy" if ok
                  else "[red]VERDICT[/red]  backend NOT healthy — see above")
    sys.exit(0 if ok else 1)


def _cfg_get_lazy(key: str):
    """config.get without importing at module scope (matches the other call sites)."""
    from .config import get as _get
    try:
        return _get(key)
    except Exception:
        return None


@tf_stacks.command("backend-check")
@click.option("--stack", default=None,
              help="Check the backend this stack resolves to (honours perStack overrides).")
def tf_backend_check(stack: str | None) -> None:
    """Diagnose the tofu state backend: creds, reachability, bucket access.

    Answers the question `tofu init` refuses to: WHY did the backend fail.
    tofu reports "No valid credential sources found" both when the secret was
    never found and when it was found and rejected — two completely different
    problems with the same message, and one of them is a one-line config fix.

    Stages, each reported separately:
      1. what backend this fleet (or one stack) resolves to
      2. where credentials came from — which SOPS file, whether the key
         existed there, and whether the environment already overrode it
      3. whether AWS accepts them (STS)
      4. whether the bucket exists and is readable
    """
    from . import config as _config
    root = find_project_root()
    backend = _config.get("backend", {}) or {}
    ok = True

    # ── 1. resolved backend ────────────────────────────────────────
    btype = backend.get("type") or "s3"
    per = backend.get("per_stack") or backend.get("perStack") or {}
    if stack:
        slug = stack.replace(".", "-")
        override = per.get(slug) or {}
        if override:
            btype = override.get("type", btype)
            console.print(f"[cyan]backend[/cyan]  {stack} overrides the fleet backend → {override}")
    console.print(f"[cyan]backend[/cyan]  type={btype}"
                  + (f" bucket={backend.get('bucket')} region={backend.get('region')}"
                     if btype == "s3" else ""))
    if btype == "local":
        console.print("[green]OK[/green]       local state — no credentials, no bucket, "
                      "nothing to check. State lives in .tf/<slug>/terraform.tfstate.")
        return
    if btype == "pg":
        _backend_check_pg(root, backend)
        return
    if per:
        console.print(f"[dim]         per-stack overrides declared for: "
                      f"{', '.join(sorted(per))}[/dim]")

    # ── 2. credential provenance ───────────────────────────────────
    # Mirrors main.py's resolution exactly, including its precedence, so this
    # reports what the launcher actually sees rather than an idealised view.
    env_id = os.environ.get("AWS_ACCESS_KEY_ID")
    if env_id:
        console.print(f"[yellow]creds[/yellow]    AWS_ACCESS_KEY_ID already in the environment "
                      f"({env_id[:4]}…{env_id[-3:]}) — the environment WINS over SOPS "
                      f"(the launcher uses setdefault).")
    # Ask for the TREE, not a file: once fleet.settings.sopsFiles routes
    # `integrations`, reporting a FAIL because the *default* file lacks it is
    # a false positive — and a diagnostic that cries wolf stops being read.
    from .config import integrations_file as _cfg_secrets
    cfg_file = Path(str(_cfg_secrets()))
    sops = shutil.which("sops")
    if not sops:
        console.print("[red]FAIL[/red]     `sops` not on PATH — run inside `nix develop`.")
        sys.exit(1)

    from .config import get as _cfg_get
    creds_path = _cfg_get("backend_s3.creds_sops_path") or '["integrations"]["aws"]'
    # Name the path as the fleet configured it; reporting "integrations.aws" at
    # a fleet that keeps its keys elsewhere sends the reader hunting for a key
    # that is not supposed to exist.
    creds_label = ".".join(re.findall(r'\["([^"]+)"\]', creds_path)) or creds_path

    def _extract(path: Path):
        r = subprocess.run([sops, "-d", "--extract", creds_path, str(path)],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None, (r.stderr or "").strip().splitlines()[-1:] or [""]
        import yaml
        return yaml.safe_load(r.stdout), None

    creds, err = (None, None)
    if cfg_file.is_file():
        creds, err = _extract(cfg_file)
    if creds:
        console.print(f"[green]creds[/green]    {creds_label} found in {cfg_file}")
    else:
        console.print(f"[red]FAIL[/red]     {creds_label} NOT in {cfg_file} "
                      f"— the file this fleet routes the integrations tree to")
        ok = False
        # A split SOPS store is the usual cause; say where it actually lives.
        for sibling in sorted(cfg_file.parent.glob("*.yaml")):
            if sibling == cfg_file:
                continue
            found, _ = _extract(sibling)
            if found:
                console.print(f"[yellow]  →[/yellow]      it IS in {sibling}. Either move the key, "
                              f"or point fleet.settings.sopsSecretsFile there.")
                creds = creds or found
                break
        else:
            # No sibling holds it under this name either. The usual cause is
            # not a missing secret but a differently-NAMED one — the fleet
            # files its state credentials under a tree of its own and never
            # told the CLI, which cannot distinguish that from absence.
            console.print("[yellow]  →[/yellow]      if this fleet files its state "
                          "credentials elsewhere, set "
                          "fleet.settings.backend.s3.credsSopsPath "
                          "(e.g. integrations.tofu.garage).")

    if not creds:
        console.print("[red]VERDICT[/red]  no credentials resolvable. Nothing to test against AWS.")
        sys.exit(1)

    # ── 3. does AWS accept them ────────────────────────────────────
    env = {**os.environ,
           "AWS_ACCESS_KEY_ID": creds["access_key_id"],
           "AWS_SECRET_ACCESS_KEY": creds["secret_access_key"],
           "AWS_DEFAULT_REGION": creds.get("region") or backend.get("region") or "us-east-1"}
    aws = shutil.which("aws") or None
    prefix = [aws] if aws else ["nix", "run", "--inputs-from", str(root), "nixpkgs#awscli2", "--"]

    r = subprocess.run(prefix + ["sts", "get-caller-identity", "--output", "json"],
                       capture_output=True, text=True, env=env, timeout=90)
    if r.returncode == 0:
        who = json.loads(r.stdout)
        console.print(f"[green]sts[/green]      accepted — {who.get('Arn')}")
    else:
        msg = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        console.print(f"[red]FAIL[/red]     STS rejected the credentials:\n         {msg[0]}")
        if "InvalidClientTokenId" in r.stderr or "InvalidAccessKeyId" in r.stderr:
            console.print("[dim]         The key ID is not known to AWS at all — deleted or "
                          "rotated upstream, not a permissions problem. Ask whoever owns the "
                          "account for a new one.[/dim]")
        console.print("[red]VERDICT[/red]  credentials resolve but are not valid.")
        sys.exit(1)

    # ── 4. bucket ──────────────────────────────────────────────────
    bucket = backend.get("bucket")
    r = subprocess.run(prefix + ["s3api", "head-bucket", "--bucket", bucket],
                       capture_output=True, text=True, env=env, timeout=90)
    if r.returncode == 0:
        console.print(f"[green]bucket[/green]   s3://{bucket} reachable and readable")
    else:
        last = (r.stderr or "").strip().splitlines()[-1:] or [""]
        console.print(f"[red]FAIL[/red]     s3://{bucket}: {last[0]}")
        console.print("[dim]         Credentials are valid, so this is the bucket or its "
                      "policy — not the key.[/dim]")
        ok = False

    console.print("[green]VERDICT[/green]  backend healthy" if ok
                  else "[red]VERDICT[/red]  backend NOT healthy — see above")
    sys.exit(0 if ok else 1)


# ── tf adopt: bring existing infrastructure under management ──────────
#
# CONFIG IS THE DRIVER. Resources are enumerated from the generated
# config.tf.json, and for each declared resource we work out what its
# real-world ID should be. Nothing undeclared can ever be pulled in — the
# blast radius is bounded by the manifest. (The inverse question, "what
# exists that I do NOT manage", is a separate verb by design; the two have
# very different risk profiles.)
#
# Every ID here is derived from fields the config already carries, so the
# derivation needs no network. It is then VERIFIED against the live
# provider before anything touches state: vmids are unique per cluster, but
# nothing guarantees the object at a vmid is the one this manifest means —
# a stale entry, a hand-edited number, or a vmid reused after a destroy all
# bind the wrong object. Deriving is cheap; being wrong is not.

# xenorchestra_vm import ID is the VM's UUID, which the config does NOT carry
# — XO assigns it at create time. The only stable, config-known handle is the
# name_label, so the "id derivation" is a live lookup: match the declared
# name_label against the pool and read back its uuid. name_label is unique per
# pool in practice but not enforced by XO; list_vms_by_name keys on it, so a
# duplicate silently collapses to one entry — acceptable here because a real
# fleet never ships two VMs with the same name_label (the emitter derives it
# from the unique resource name).
_xoa_vms_by_name: dict | None = None


def _xoa_vm_uuid(name: str, body: dict) -> str | None:
    """Resolve a declared xenorchestra_vm to its live UUID via the XO API.

    Raises RuntimeError if XO is unreachable — a lookup that could not RUN is
    not the same answer as a VM that does not EXIST, and the caller must tell
    them apart. Returns the uuid on a match, or None when no VM carries the
    declared name_label (genuinely not provisioned yet)."""
    global _xoa_vms_by_name
    if _xoa_vms_by_name is None:
        from . import xoa_api
        by_name = xoa_api.list_vms_by_name()
        if by_name is None:
            raise RuntimeError("XO REST API unreachable (check XOA_URL/XOA_TOKEN)")
        _xoa_vms_by_name = by_name
    vm = _xoa_vms_by_name.get(body.get("name_label") or name)
    return vm.get("uuid") if vm else None


# xenorchestra_cloud_config: same story as the VM — XO mints the UUID, and the
# only config-known handle is the config's `name` (the emitter sets it to
# "<resource>-cloud-init"). Unlike VMs, cloud-config names are NOT unique in a
# live XO (hand-made configs, other tools, and re-created sandboxes collide), so
# this keeps a name -> {ids} multimap and REFUSES a name that resolves to more
# than one live object rather than binding an arbitrary id. The list is only on
# XO's JSON-RPC (`cloudConfig.getAll`), not the REST surface xoa_api speaks, so
# it goes through the xoa-cli websocket client (already a launcher dependency).
_xoa_cc_ids_by_name: dict | None = None


def _xoa_cloud_config_id(name: str, body: dict) -> str | None:
    """Resolve a declared xenorchestra_cloud_config to its live UUID.

    Raises RuntimeError if XO is unreachable (unknown, not absent) and
    ValueError if the declared name matches more than one live config
    (ambiguous — never guessed). Returns the uuid on a unique match, or None
    when nothing carries the name (not provisioned yet)."""
    global _xoa_cc_ids_by_name
    if _xoa_cc_ids_by_name is None:
        from xoa_cli.api import XoRpc
        try:
            with XoRpc() as rpc:
                configs = rpc.call("cloudConfig.getAll")
        except Exception as e:
            raise RuntimeError(f"XO JSON-RPC unreachable: {e}") from e
        acc: dict = {}
        for c in configs or []:
            if isinstance(c, dict) and c.get("name") and c.get("id"):
                acc.setdefault(c["name"], set()).add(c["id"])
        _xoa_cc_ids_by_name = acc
    label = body.get("name") or f"{name}-cloud-init"
    ids = _xoa_cc_ids_by_name.get(label) or set()
    if len(ids) > 1:
        raise ValueError(f"{len(ids)} live cloud configs named {label!r} — "
                         "ambiguous, refusing to guess")
    return next(iter(ids), None)


# cloudflare_record: the import id is <zone_id>/<record_id>, and NEITHER half
# is in config — the provider/Cloudflare mint both. The record body carries
# name/type/content plus a zone_id that is a data-source interpolation
# ("${data.cloudflare_zone.<key>.id}"), not a literal. So resolve the zone name
# from that <key> (via the data.cloudflare_zone map captured in _adopt_rows),
# look the zone id up by name, then the record id up by name+type+content.
# Cloudflare record names are unique per (name, type, content), so a match on
# all three is exact; anything ambiguous is refused, never guessed.
_cf_zone_names_by_key: dict = {}  # data.cloudflare_zone <key> -> zone name; set by _adopt_rows


def _cloudflare_record_id(name: str, body: dict) -> str | None:
    """Resolve a declared cloudflare_record to its import id (zone_id/record_id).

    Raises RuntimeError if Cloudflare is unreachable or the zone/token is
    missing (unknown, not absent) and ValueError if name+type+content matches
    more than one live record (ambiguous — never guessed). Returns the import
    id on a unique match, or None when nothing matches (not provisioned yet)."""
    from . import cloudflare_api
    zid_ref = str(body.get("zone_id") or "")
    m = re.search(r"cloudflare_zone\.([A-Za-z0-9_]+)\.id", zid_ref)
    zone_name = _cf_zone_names_by_key.get(m.group(1)) if m else None
    if not zone_name:
        raise RuntimeError(f"cannot resolve zone name (zone_id={zid_ref!r})")
    try:
        zid = cloudflare_api.zone_id(zone_name)
    except cloudflare_api.CloudflareError as e:
        raise RuntimeError(str(e)) from e
    if not zid:
        raise RuntimeError(f"token cannot see zone {zone_name!r}")
    sub = str(body.get("name") or "")
    fqdn = zone_name if sub in ("", "@") else f"{sub}.{zone_name}"
    rtype = str(body.get("type") or "A")
    content = str(body.get("content") or "")
    try:
        recs = cloudflare_api.list_dns_records(zid, fqdn, rtype)
    except cloudflare_api.CloudflareError as e:
        raise RuntimeError(str(e)) from e
    if not recs:
        return None                              # genuinely not provisioned yet
    if len(recs) == 1:
        # The one record at this name+type IS the object this entry manages.
        # If its content has drifted from config, that surfaces as an in-place
        # update in the post-adopt plan (--allow-updates) — not a reason to
        # refuse the import, and never a reason to bind a different object.
        return f"{zid}/{recs[0]['id']}"
    # Multiple records share this name+type (round-robin). content picks the one
    # this entry means; if it can't pick exactly one, refuse rather than guess.
    matched = [r for r in recs if r.get("content") == content]
    if len(matched) == 1:
        return f"{zid}/{matched[0]['id']}"
    raise ValueError(f"{len(recs)} live {rtype} records at {fqdn!r}, "
                     f"{len(matched)} match content {content!r} — ambiguous, "
                     "refusing to guess")


def _proxmox_acl_id(name: str, body: dict) -> str | None:
    """bpg proxmox_acl import id: {path}?{principal}?{role} (bpg docs). The
    principal is a bare group name, user@realm, or user@realm!token — exactly
    the field the config already carries, used verbatim (no realm to guess)."""
    path = body.get("path")
    role = body.get("role_id")
    principal = body.get("group_id") or body.get("user_id") or body.get("token_id")
    if not (path and role and principal):
        return None
    return f"{path}?{principal}?{role}"


# grafana_folder / grafana_rule_group / grafana_synthetic_monitoring_check all
# import by a Grafana-assigned id the config does not carry (folder uid, SM
# check numeric id), so they resolve via the Grafana + SM APIs. The folder <key>
# -> title map is captured in _adopt_rows so the rule_group resolver can turn a
# ${grafana_folder.<key>.uid} reference into the title to look the uid up by.
_grafana_folder_titles_by_key: dict = {}  # grafana_folder <resource key> -> title


def _grafana_folder_uid(name: str, body: dict) -> str | None:
    from . import grafana_api
    title = body.get("title")
    if not title:
        raise RuntimeError("grafana_folder has no title in config")
    try:
        return grafana_api.folder_uid(title)
    except grafana_api.GrafanaError as e:
        raise RuntimeError(str(e)) from e


def _grafana_rule_group_id(name: str, body: dict) -> str | None:
    from . import grafana_api
    grp = body.get("name")
    if not grp:
        raise RuntimeError("grafana_rule_group has no name in config")
    ref = str(body.get("folder_uid") or "")
    m = re.search(r"grafana_folder\.([A-Za-z0-9_]+)\.uid", ref)
    ftitle = _grafana_folder_titles_by_key.get(m.group(1)) if m else None
    if not ftitle:
        raise RuntimeError(f"cannot resolve folder title (folder_uid={ref!r})")
    try:
        fuid = grafana_api.folder_uid(ftitle)
    except grafana_api.GrafanaError as e:
        raise RuntimeError(str(e)) from e
    if not fuid:
        return None  # folder not created yet — rule group cannot exist either
    return f"{fuid}:{grp}"


def _grafana_sm_check_id(name: str, body: dict) -> str | None:
    from . import grafana_api
    job = body.get("job")
    target = body.get("target")
    if not (job and target):
        raise RuntimeError("grafana_synthetic_monitoring_check missing job/target")
    try:
        return grafana_api.sm_check_id(job, target)
    except grafana_api.GrafanaError as e:
        raise RuntimeError(str(e)) from e


# type -> (id_fn(name, body) -> str | None, kind)
# `kind` is one of:
#   True     — id is DERIVED from config fields, then VERIFIED against the live
#              provider before use (containers/vms: the vmid is unique but could
#              still point at the wrong object; the check confirms identity).
#   False    — id is DERIVED from config and its FORMAT is certain, but there is
#              no cheap live check (pools/groups: the id simply IS the name).
#   "lookup" — id is NOT in the config at all; it must be FETCHED from the
#              provider by a stable natural key (xenorchestra_vm: XO mints the
#              UUID at create time, so we match on name_label). The fetch is both
#              the derivation and the verification — a miss means the object does
#              not exist; an unreachable provider means the answer is unknown.
# Only types whose import-ID is derivable this way live here. A guessed ID that
# happens to parse is the worst outcome available: it binds an address to some
# other real object, and the next apply "corrects" that object to match the
# config. Unknown types are reported as manual, never guessed.
_ADOPT_RESOLVERS: dict = {
    "proxmox_virtual_environment_container":
        (lambda n, b: f"{b['node_name']}/{b['vm_id']}" if b.get("vm_id") else None, True),
    "proxmox_virtual_environment_vm":
        (lambda n, b: f"{b['node_name']}/{b['vm_id']}" if b.get("vm_id") else None, True),
    "proxmox_virtual_environment_pool":
        (lambda n, b: b.get("pool_id"), False),
    "proxmox_virtual_environment_group":
        (lambda n, b: b.get("group_id"), False),
    "xenorchestra_vm": (_xoa_vm_uuid, "lookup"),
    "xenorchestra_cloud_config": (_xoa_cloud_config_id, "lookup"),
    "cloudflare_record": (_cloudflare_record_id, "lookup"),
    # bpg cluster options are a global singleton — import id is the constant
    # "cluster" (bpg docs), no per-resource derivation.
    "proxmox_cluster_options": (lambda n, b: "cluster", False),
    "proxmox_acl": (_proxmox_acl_id, False),
    # grafana alerting objects import by their config-declared name (the
    # provider scopes to its own org); no Grafana-assigned id to look up.
    "grafana_contact_point": (lambda n, b: b.get("name"), False),
    "grafana_message_template": (lambda n, b: b.get("name"), False),
    "grafana_folder": (_grafana_folder_uid, "lookup"),
    "grafana_rule_group": (_grafana_rule_group_id, "lookup"),
    "grafana_synthetic_monitoring_check": (_grafana_sm_check_id, "lookup"),
}

# No real-world counterpart: nothing to adopt, ever. Called out explicitly
# because they are silently RE-CREATED on the next apply — and a resource
# that generates a credential would rotate a live secret when it is.
# ansible_host/ansible_playbook are local inventory state + a playbook runner
# (no remote object, no import support in the provider), so they live here too.
_UNIMPORTABLE = {"terraform_data", "random_password", "random_id",
                 "random_string", "tls_private_key",
                 "ansible_host", "ansible_playbook"}

# The subset of _UNIMPORTABLE that is safe to leave PENDING during an adopt: it
# holds no state worth preserving and generates no secret, so its create in the
# post-import plan is inherent (nothing to import) rather than a wrong-id
# signal, and adopt leaves it for a later real apply instead of executing it.
# terraform_data (provisioner runners — e.g. the XO boot-order setters) and the
# ansible_* resources (inventory + playbook runner, fully reconstructed from
# config) belong here; random_*/tls_private_key do NOT — recreating those
# rotates a live value.
_RECREATE_SAFE = {"terraform_data", "ansible_host", "ansible_playbook"}


def _unwrap(block):
    """terranix emits a resource body as either a dict or a 1-element list."""
    return block[0] if isinstance(block, list) else block


def _expected_hostname(name: str, body: dict) -> str:
    init = _unwrap(body.get("initialization") or {}) or {}
    return init.get("hostname") or name


def _verify_pve(vmid: int, node: str, expect: str) -> tuple[bool, str]:
    """Confirm the object at `vmid` is the one the config means."""
    try:
        from . import pve_api
        api = pve_api.get_client()
        cfg = pve_api.get_container_config(api, int(vmid), node=node)
    except BaseException as e:
        # BaseException, not Exception: pve_api exits the process when
        # PROXMOX_VE_* is unset, and a missing credential must degrade this
        # check rather than abort an otherwise useful dry run.
        msg = f"{type(e).__name__}: {str(e)[:100]}".strip()
        # "the object is not there" and "I could not look" are DIFFERENT
        # answers. Reporting the second as the first would tell an operator a
        # container is missing when the truth is that a credential is — and
        # the fix for each is nothing like the fix for the other.
        if any(s in str(e).lower() for s in ("does not exist", "not found", "no such")):
            return "absent", msg
        return "unverified", msg
    actual = (cfg or {}).get("hostname") or ""
    if not actual:
        return "unverified", "live object has no hostname field"
    if actual != expect:
        return "mismatch", f"live hostname is {actual!r}, config says {expect!r}"
    return "ok", actual


def _adopt_rows(wd: Path, verify: bool, in_state: set[str]) -> list[dict]:
    cfg = json.loads((wd / "config.tf.json").read_text())
    # cloudflare_record bodies reference their zone via a data-source
    # interpolation, not a literal id, so capture the data.cloudflare_zone
    # <key> -> zone name map here for _cloudflare_record_id to dereference.
    global _cf_zone_names_by_key
    _cf_zone_names_by_key = {
        k: _unwrap(v).get("name")
        for k, v in ((cfg.get("data") or {}).get("cloudflare_zone") or {}).items()
    }
    # grafana_rule_group references its folder as ${grafana_folder.<key>.uid};
    # capture <key> -> title so the resolver can look the live uid up by title.
    global _grafana_folder_titles_by_key
    _grafana_folder_titles_by_key = {
        k: _unwrap(v).get("title")
        for k, v in ((cfg.get("resource") or {}).get("grafana_folder") or {}).items()
    }
    # The Grafana + SM API base URLs are literals in the provider block (only
    # the tokens are SOPS-injected, and those the bootstrap already exports).
    # Surface the URLs to grafana_api without a second config source of truth.
    gf = (cfg.get("provider") or {}).get("grafana")
    gf = _unwrap(gf) if gf else None
    if isinstance(gf, dict):
        if gf.get("url"):
            os.environ.setdefault("GRAFANA_URL", str(gf["url"]))
        if gf.get("sm_url"):
            os.environ.setdefault("GRAFANA_SM_URL", str(gf["sm_url"]))
    rows: list[dict] = []
    for rtype, entries in (cfg.get("resource") or {}).items():
        for name, block in entries.items():
            addr = f"{rtype}.{name}"
            body = _unwrap(block) or {}
            if rtype in _UNIMPORTABLE:
                rows.append({"addr": addr, "id": "", "state": "unimportable",
                             "note": "no real-world object — recreated on apply"})
                continue
            if addr in in_state:
                rows.append({"addr": addr, "id": "", "state": "in-state",
                             "note": "already managed"})
                continue
            resolver = _ADOPT_RESOLVERS.get(rtype)
            if resolver is None:
                rows.append({"addr": addr, "id": "", "state": "manual",
                             "note": f"no resolver for {rtype} — import by hand"})
                continue
            id_fn, kind = resolver

            # A "lookup" resolver reaches the provider: the import ID is not in
            # config (the provider mints it), so the fetch IS the derivation and
            # the verification at once. Under --no-verify there is nothing left
            # to go on, so the address is punted to manual rather than guessed.
            if kind == "lookup":
                if not verify:
                    rows.append({"addr": addr, "id": "", "state": "manual",
                                 "note": "provider-assigned id — needs a live "
                                         "lookup, refused under --no-verify"})
                    continue
                try:
                    rid = id_fn(name, body)
                except Exception as e:
                    # The lookup could not yield a confirmed single id — either
                    # the provider was unreachable (unknown, NOT absent) or the
                    # key was ambiguous. Never adopt on a guess; surface the
                    # reason so the operator fixes creds or the collision.
                    rows.append({"addr": addr, "id": "", "state": "unverified",
                                 "note": f"lookup inconclusive: {e}"})
                    continue
                if not rid:
                    rows.append({"addr": addr, "id": "", "state": "not-found",
                                 "note": "no live object matches the declared name"})
                    continue
                rows.append({"addr": addr, "id": rid, "state": "adopt",
                             "note": "resolved via provider API"})
                continue

            try:
                rid = id_fn(name, body)
            except Exception as e:
                rid = None
                rows.append({"addr": addr, "id": "", "state": "manual",
                             "note": f"could not derive id: {e}"})
                continue
            if not rid:
                rows.append({"addr": addr, "id": "", "state": "manual",
                             "note": "id fields absent from config"})
                continue
            if verify and kind and "/" in str(rid):
                node, vmid = str(rid).split("/", 1)
                verdict, detail = _verify_pve(vmid, node, _expected_hostname(name, body))
                if verdict == "absent":
                    # The normal path for a resource that has not been
                    # provisioned yet — a plain apply will create it.
                    rows.append({"addr": addr, "id": rid, "state": "not-found",
                                 "note": "not provisioned yet — apply will create it"})
                    continue
                if verdict == "mismatch":
                    rows.append({"addr": addr, "id": rid, "state": "mismatch",
                                 "note": detail})
                    continue
                if verdict == "unverified":
                    rows.append({"addr": addr, "id": rid, "state": "unverified",
                                 "note": detail})
                    continue
            rows.append({"addr": addr, "id": rid, "state": "adopt",
                         "note": "verified" if (verify and kind) else "derived"})
    return sorted(rows, key=lambda r: r["addr"])


@tf_stacks.command("adopt")
@click.argument("scope")
@click.option("--target", "targets", multiple=True,
              help="Limit to these resource addresses (repeatable).")
@click.option("--yes", is_flag=True, help="Actually adopt. Without this, dry-run only.")
@click.option("--merge", is_flag=True,
              help="Allow adopting into a stack that already holds state.")
@click.option("--no-verify", is_flag=True,
              help="Skip the live provider check. Faster, and strictly more dangerous.")
@click.option("--allow-updates", "allow_updates", is_flag=True,
              help="Permit in-place updates in the post-import plan. Never permits "
                   "create/destroy/replace. Needed for providers whose importer "
                   "under-populates attributes (bpg: timeouts, vm_id).")
def tf_adopt(scope: str, targets: tuple[str, ...], yes: bool,
             merge: bool, no_verify: bool, allow_updates: bool) -> None:
    """Rebuild tofu state from live infrastructure for matched leaves.

    Disaster recovery, and what makes a local backend survivable: if state can
    be reconstructed from reality on demand, losing a state file stops being
    an emergency.

    Dry-run by default, and the dry-run deliberately does NOT need a working
    state backend — the moment you most want to know what is adoptable is
    when the backend is unreachable.

    Adoption makes state match reality BY DEFINITION, so any pre-existing
    drift silently becomes the new baseline. Read the dry-run table.
    """
    root = find_project_root()
    overall = 0
    for leaf in _resolve_scope(root, scope):
        console.print(f"── adopt {leaf} ──", style="bold cyan")
        wd = _stage_json(root, leaf)

        # State is only consulted when we can reach it. A dry-run against an
        # unreachable backend is still useful, so degrade rather than abort.
        in_state: set[str] = set()
        state_known = False
        if yes or (wd / ".terraform").is_dir():
            try:
                _ensure_init(wd)
                in_state = set(_state_addresses(wd))
                state_known = True
            except Exception:
                if yes:
                    console.print("[red]ERROR:[/red] cannot reach the state backend; "
                                  "adoption needs to write state. Try "
                                  "`fleet deploy tf backend-check`.")
                    sys.exit(1)
        if not state_known:
            console.print("[dim]state backend not consulted — 'in-state' cannot be "
                          "detected in this run[/dim]")

        rows = _adopt_rows(wd, verify=not no_verify, in_state=in_state)
        if targets:
            rows = [r for r in rows if r["addr"] in targets]
        if not rows:
            console.print("  nothing to consider")
            continue

        table = Table(show_header=True, header_style="bold")
        table.add_column("resource"); table.add_column("import id")
        table.add_column("status"); table.add_column("note")
        colour = {"adopt": "green", "in-state": "dim", "not-found": "yellow",
                  "mismatch": "red", "manual": "yellow", "unimportable": "red",
                  "unverified": "yellow"}
        for r in rows:
            c = colour.get(r["state"], "")
            table.add_row(r["addr"], r["id"], f"[{c}]{r['state']}[/{c}]" if c else r["state"],
                          r["note"])
        console.print(table)

        adoptable = [r for r in rows if r["state"] == "adopt"]
        mismatched = [r for r in rows if r["state"] == "mismatch"]
        unverified = [r for r in rows if r["state"] == "unverified"]
        if unverified:
            console.print(f"[yellow]WARNING:[/yellow] {len(unverified)} resource(s) could not be "
                          "checked against the live provider — that is not the same as absent. "
                          "They are excluded from adoption; fix provider access, or accept the "
                          "risk explicitly with --no-verify.")
        if mismatched:
            console.print(f"[red]REFUSING {leaf}:[/red] {len(mismatched)} resource(s) resolve to a "
                          "live object that is NOT the one the config describes. Adopting these "
                          "would bind the wrong object and the next apply would rewrite it. Fix "
                          "the manifest (or pass --target to adopt only the good ones).")
            overall = 1
            continue
        if not adoptable:
            console.print("  nothing adoptable")
            continue
        if not yes:
            console.print(f"[bold]dry run[/bold] — {len(adoptable)} resource(s) would be adopted. "
                          f"Re-run with --yes to write state.")
            continue
        if in_state and not merge:
            console.print(f"[red]REFUSING {leaf}:[/red] state already holds "
                          f"{len(in_state)} resource(s). Adopting into a non-empty state can "
                          "double-bind an address — pass --merge if that is what you mean.")
            overall = 1
            continue

        # Import BLOCKS, not N× `tofu import`: batched, and the plan becomes
        # the review artifact. Staged as a separate file so generated config
        # is never polluted, and removed on the way out.
        imports = [{"to": r["addr"], "id": r["id"]} for r in adoptable]
        imp = wd / "zz-adopt-import.tf.json"
        imp.write_text(json.dumps({"import": imports}, indent=2))
        try:
            backup = wd / "terraform.tfstate.pre-adopt"
            src = wd / "terraform.tfstate"
            if src.is_file():
                shutil.copy2(src, backup)
                console.print(f"[dim]state backed up → {backup}[/dim]")

            # THE GATE. apply performs the imports AND anything else in the
            # plan, so a wrong id does not fail loudly — it binds the wrong
            # object and the same apply "corrects" it. Refuse unless the plan
            # is imports and nothing else.
            plan = wd / "adopt.tfplan"
            r = subprocess.run(["tofu", "plan", "-input=false", "-out", str(plan)],
                               cwd=wd, capture_output=True, text=True)
            if r.returncode != 0:
                console.print(f"[red]ERROR:[/red] plan failed:\n{(r.stderr or '')[-800:]}")
                overall = 1
                continue
            show = subprocess.run(["tofu", "show", "-json", str(plan)],
                                  cwd=wd, capture_output=True, text=True)
            all_changes = [c for c in json.loads(show.stdout).get("resource_changes", [])
                           if set(c.get("change", {}).get("actions", [])) - {"no-op"}]
            # Separate by DANGER, not by "is there a diff at all".
            #
            # The gate exists to catch a WRONG ID — one that binds an address
            # to some other real object, which the same apply then rewrites.
            # That shows up as destroy, replace, or a resource this run failed
            # to adopt and would now CREATE a duplicate of.
            #
            # A pure in-place update is different, and unavoidable here: the
            # bpg importer does not populate client-side timeouts or vm_id, so
            # EVERY Proxmox adoption plans an update that touches no API. A
            # gate that refuses those can never pass, which makes it useless
            # rather than strict — so they are allowed behind an explicit flag
            # and always printed.
            #
            # A CREATE of a recreate-safe unimportable type (terraform_data:
            # provisioner runners like the XO boot-order setters) is INHERENT —
            # it has no real object to import, so it can never be "already
            # adopted". It is neither a wrong id nor a rotated secret, so it does
            # not gate; it is left PENDING (the apply below targets only the
            # imports, so adopt never executes it — a later real apply does).
            def _acts(c):
                return set(c.get("change", {}).get("actions", [])) - {"no-op"}
            pending_safe = [c for c in all_changes
                            if _acts(c) == {"create"} and c.get("type") in _RECREATE_SAFE]
            safe_addrs = {c["address"] for c in pending_safe}
            unsafe = [c for c in all_changes
                      if c["address"] not in safe_addrs and _acts(c) & {"delete", "create"}]
            updates = [c for c in all_changes if _acts(c) == {"update"}]
            changes = list(unsafe) if allow_updates else (unsafe + updates)
            if updates and allow_updates:
                console.print(f"[yellow]note:[/yellow] {len(updates)} in-place update(s) "
                              "in the plan — expected for this provider (timeouts, vm_id). "
                              "Review before the next apply.")
            if pending_safe:
                console.print(f"[yellow]note:[/yellow] {len(pending_safe)} unimportable "
                              "resource(s) (e.g. boot-order runners) left PENDING — created by "
                              "the next apply, not by adopt.")
            if changes:
                console.print(f"[red]REFUSING {leaf}:[/red] the plan contains "
                              f"{len(changes)} resource change(s) beyond the imports:")
                for c in changes[:10]:
                    console.print(f"    {'/'.join(c['change']['actions'])}  {c['address']}")
                console.print("  Adoption must be a pure state operation. A non-empty plan means "
                              "an id is wrong or the config has drifted — resolve that first.")
                overall = 1
                continue
            # Apply ONLY the imports. When the plan carries pending_safe creates,
            # re-plan targeted to the adoptable addresses so those creates are
            # NOT executed here; otherwise the saved full plan is imports-only.
            if pending_safe:
                targets = [f"-target={r['addr']}" for r in adoptable]
                r = subprocess.run(
                    ["tofu", "plan", "-input=false", "-out", str(plan), *targets],
                    cwd=wd, capture_output=True, text=True)
                if r.returncode != 0:
                    console.print(f"[red]ERROR:[/red] targeted import plan failed:\n"
                                  f"{(r.stderr or '')[-800:]}")
                    overall = 1
                    continue
            r = subprocess.run(["tofu", "apply", "-input=false", str(plan)],
                               cwd=wd, capture_output=True, text=True)
            if r.returncode != 0:
                console.print(f"[red]ERROR:[/red] adopt failed:\n{(r.stderr or '')[-800:]}")
                overall = 1
                continue
            console.print(f"[green]adopted[/green] {len(adoptable)} resource(s) into {leaf}")
        finally:
            imp.unlink(missing_ok=True)
            (wd / "adopt.tfplan").unlink(missing_ok=True)

    sys.exit(overall)
