"""fleet — unified CLI launcher for fleetkit-managed infrastructure.

Usage:
    fleet                  -> Trogon TUI (interactive command browser)
    fleet tui              -> Explicit Trogon TUI launch
    fleet deploy ...       -> Deployment pipelines (nixos / tf)
    fleet inventory ...    -> Host inventory management
    fleet remote ...       -> Run commands on fleet hosts via SSH or pct exec
    fleet bootstraps ...   -> One-time setup (pve / step-ca)
    fleet devtools ...     -> Secrets, utilities
"""
from __future__ import annotations

import click

# Trogon is a venv dependency (pyproject.toml), not a Nix package.
# Gracefully degrade if unavailable (e.g. running as a standalone Nix build).
try:
    from trogon import tui as _tui_decorator

    _HAS_TROGON = True

    # Trogon doesn't filter Click's UNSET sentinel, so required options
    # with no default show "Sentinel.UNSET" in the TUI form fields.
    # Patch the introspection to normalize UNSET → None.
    try:
        from click._utils import UNSET as _CLICK_UNSET
        import trogon.introspect as _trogon_introspect
        _orig_process = _trogon_introspect.MultiValueParamData.process_cli_option

        @classmethod  # type: ignore[misc]
        def _patched_process(cls, default):
            if default is _CLICK_UNSET:
                default = None
            return _orig_process(default)

        _trogon_introspect.MultiValueParamData.process_cli_option = _patched_process
    except (ImportError, AttributeError):
        pass  # Click internals changed — silently skip

except ImportError:
    _HAS_TROGON = False

    def _tui_decorator():  # type: ignore[misc]
        """No-op decorator when trogon is not installed."""
        def _identity(fn):
            return fn
        return _identity

# ── Local sub-groups ──────────────────────────────────────────
from fleet_launcher.deploy_group import deploy
from fleet_launcher.inventory import inventory as inventory_cli
from fleet_launcher.bootstraps_group import bootstraps
from fleet_launcher.devtools_group import devtools
from fleet_launcher.remote import remote
from fleet_launcher.sessions import sessions_cli
from fleet_launcher.pki_group import pki
from fleet_launcher.ansible_group import ansible as ansible_cli
from fleet_launcher.mcp_group import mcp as mcp_cli


# ── Root group ────────────────────────────────────────────────

@_tui_decorator()
@click.group(invoke_without_command=True)
@click.version_option("0.3.0", prog_name="fleet")
@click.pass_context
def fleet(ctx: click.Context) -> None:
    """fleetkit infrastructure tool suite.

    Unified CLI for managing Proxmox LXC containers (via terranix/tofu),
    NixOS deployments (via Colmena), secrets, and developer tooling. Run
    without arguments to open the interactive TUI (requires Trogon), or
    use subcommands directly.

    \b
    Top-level groups:
      deploy       Deployment pipelines (nixos / tf)
      inventory    Host inventory generation and management
      remote       Run commands on fleet hosts (SSH or pct exec)
      bootstraps   One-time setup (pve/step-ca)
      devtools     Secrets, utilities
    """
    if ctx.invoked_subcommand is None:
        if _HAS_TROGON:
            # Trogon adds a "tui" command — invoke it automatically.
            ctx.invoke(fleet.commands["tui"])
        else:
            click.echo(ctx.get_help())


# ── Top-level groups ──────────────────────────────────────────

fleet.add_command(deploy)
fleet.add_command(mcp_cli, "mcp")
fleet.add_command(inventory_cli, "inventory")
fleet.add_command(bootstraps)
fleet.add_command(devtools)
fleet.add_command(remote)
fleet.add_command(sessions_cli, "sessions")
fleet.add_command(pki)
fleet.add_command(ansible_cli, "ansible")

from .xoa_group import xoa as _xoa
fleet.add_command(_xoa)

# `fleet pve` carries Proxmox VE host-management subcommands. The legacy
# install-nix / build-template ops in pve.py live alongside the newer
# `cluster` group for cluster lifecycle (pvecm create/add).
from .pve import pve as _pve
from .pve_cluster import cluster as _pve_cluster
_pve.add_command(_pve_cluster)
fleet.add_command(_pve)

# `fleet pbs` — Proxmox Backup Server operator commands. Single-host
# product (no cluster), so no nested `cluster` group.
from .pbs import pbs as _pbs
fleet.add_command(_pbs)

# `fleet s3` — Garage / object-store operator commands (mint-key, etc.).
from .s3 import s3 as _s3
fleet.add_command(_s3)

# ── Component families (CLIs resolve upward) ──────────────────
# Each component family exposes a COMMANDS/ATTACH manifest; register_cli_manifest
# folds it onto the root, the same code path consumer cli-ext modules use.
# Registered at MODULE level (not in main()) so --dump-verbs sees the full
# surface. The images family gives `fleet images` (build) and `fleet templates`
# (register per platform), folded in from the former fleetkit-deployer.
from .config import register_cli_manifest as _register_cli_manifest
from .components import images as _images_family
_register_cli_manifest(fleet, _images_family, source="images family")



def _find_sops() -> str | None:
    """Locate the sops binary, checking PATH and common Nix store locations."""
    import shutil
    import os
    if sops := shutil.which("sops"):
        return sops
    for candidate in [
        os.path.expanduser("~/.nix-profile/bin/sops"),
        "/run/current-system/sw/bin/sops",
        "/nix/var/nix/profiles/default/bin/sops",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return None


def _ensure_sops_age_key() -> None:
    """Set SOPS_AGE_KEY_FILE from ~/.ssh/sops-age.key if no age key is in env."""
    import os
    if os.environ.get("SOPS_AGE_KEY") or os.environ.get("SOPS_AGE_KEY_FILE"):
        return
    from .config import age_key_file
    key_file = age_key_file()
    if os.path.isfile(key_file):
        os.environ["SOPS_AGE_KEY_FILE"] = key_file


def _setenv_if_blank(name: str, value) -> None:
    """Set an env var unless it already holds a NON-EMPTY value.

    `os.environ.setdefault` is wrong here: a devshell that pre-exports
    placeholders (PROXMOX_VE_ENDPOINT="" and friends) satisfies setdefault,
    so the value decrypted from SOPS is silently discarded and the caller
    later fails with "not set" about a variable that is, in fact, set — to
    nothing. Precedence is unchanged: a real value already in the
    environment still wins.
    """
    import os  # `os` is imported inside _setup_env, not at module scope
    if not os.environ.get(name):
        os.environ[name] = str(value)


def _sops_file_for(sops_path: str, default_file: str) -> str:
    """The SOPS file owning the tree a `--extract` path names.

    A configurable sops path names its own top-level tree — `["dbs"]…`,
    `["integrations"]…` — so route on that rather than assuming the
    integrations file. Guessing fails SILENTLY: the extract returns non-zero,
    the caller's `except` swallows it, and the missing credential surfaces
    later as an unrelated-looking tofu error.
    """
    import re
    from .config import file_for as _file_for
    m = re.match(r'\["([^"]+)"\]', sops_path)
    return str(_file_for(m.group(1))) if m else default_file


def _setup_env() -> None:
    """Load .env and populate the env vars our tools consume.

    Sets:
      - AWS_* (credentials for the tofu S3 state backend; bucket from fleet.settings.backend)
      - PROXMOX_VE_* (terranix Proxmox provider credentials)

    All values come from SOPS (`nix/secrets/secrets.yaml`). Works both
    inside and outside `nix develop` — finds sops from common Nix store
    locations and loads the age key from ~/.ssh/sops-age.key if not set.
    """
    import os
    import subprocess

    try:
        from dotenv import load_dotenv
        from ._util import find_project_root
        root = find_project_root()
        env_file = root / ".env"
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    except ImportError:
        from ._util import find_project_root
        root = find_project_root()

    _ensure_sops_age_key()

    # ── fleetkit-managed runtime env ──────────────────────────────
    # Paths the toolchain (tofu, its ansible provider, sops) needs.
    # These are FRAMEWORK conventions, not user configuration: the
    # ansible tree ships with fleetkit (resolved via FLEET_ANSIBLE_DIR
    # or the repo checkout), the inventory is GENERATED from the fleet
    # manifest (`fleet ansible inventory` → .cache/fleet/
    # ansible-inventory.yml — no static inventory exists), and the
    # plugin cache is a plain performance win. Everything is setdefault
    # so an operator export still overrides. A consumer repo may carry
    # its own ansible/ tree; its config and roles take precedence over
    # the framework's.
    from .ansible_group import framework_ansible_dir
    consumer_ansible = root / "ansible"
    framework_ansible = framework_ansible_dir()
    roles_paths = [p for p in (
        consumer_ansible / "roles",
        (framework_ansible / "roles") if framework_ansible else None,
    ) if p is not None and p.is_dir()]
    if roles_paths:
        os.environ.setdefault(
            "ANSIBLE_ROLES_PATH", ":".join(str(p) for p in roles_paths))
    for cfg in (consumer_ansible / "ansible.cfg",
                (framework_ansible / "ansible.cfg") if framework_ansible else None):
        if cfg is not None and cfg.is_file():
            os.environ.setdefault("ANSIBLE_CONFIG", str(cfg))
            break
    from ._util import fleet_cache_dir
    # Inventory: the GENERATED manifest-derived inventory first, then the
    # consumer's ansible/inventory/ directory when it exists — that is
    # where a consumer keeps group_vars/ + host_vars/ (+ a static.yml for
    # out-of-manifest boxes), and ansible only loads those var dirs from
    # inventory (or playbook) locations. Later sources win merges, so
    # consumer data overrides generated groups on conflict.
    inventory_sources = [str(fleet_cache_dir(root) / "ansible-inventory.yml")]
    if (consumer_ansible / "inventory").is_dir():
        inventory_sources.append(str(consumer_ansible / "inventory"))
    os.environ.setdefault("ANSIBLE_INVENTORY", ",".join(inventory_sources))
    os.environ.setdefault("ANSIBLE_HOST_KEY_CHECKING", "False")
    # Stable anchor for consumer inventory vars: with chained inventories
    # ansible's inventory_dir is ambiguous (it names the source of the
    # CURRENT host — usually the generated file's dir), so group_vars
    # path values anchor on this instead: lookup('env','FLEET_REPO_ROOT').
    os.environ.setdefault("FLEET_REPO_ROOT", str(root))
    os.environ.setdefault(
        "TF_PLUGIN_CACHE_DIR",
        os.path.expanduser("~/.cache/opentofu/plugin-cache"))
    try:
        os.makedirs(os.environ["TF_PLUGIN_CACHE_DIR"], exist_ok=True)
    except OSError:
        # Read-only or sandboxed HOME (CI, nix build): tofu silently
        # ignores a missing cache dir, so degrade the same way.
        pass

    sops = _find_sops()
    if not sops:
        return

    # Provider credentials (integrations.*) may live in a different SOPS file
    # from the NixOS default — see config.integrations_file(). Reading the
    # wrong one fails SILENTLY below (`except: pass`), which is how a split
    # store presented as "No valid credential sources found" for days.
    from .config import integrations_file as _cfg_secrets
    secrets_file = str(_cfg_secrets())

    # AWS credentials for the tofu S3 state backend. The path is a setting
    # (`fleet.settings.backend.s3.credsSopsPath`) because "integrations.aws" is
    # a site opinion, not a fact about the backend — a fleet on Garage or MinIO
    # files those keys under its own tree, and the hardcoded name left it with
    # no credentials and no explanation.
    try:
        from .config import get as _cfg_get
        aws_path = _cfg_get("backend_s3.creds_sops_path") or '["integrations"]["aws"]'
        result = subprocess.run(
            [sops, "-d", "--extract", aws_path, _sops_file_for(aws_path, secrets_file)],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            import yaml
            aws = yaml.safe_load(result.stdout)
            _setenv_if_blank("AWS_ACCESS_KEY_ID", aws["access_key_id"])
            _setenv_if_blank("AWS_SECRET_ACCESS_KEY", aws["secret_access_key"])
            # Optional: S3-compatible stores frequently have no meaningful
            # region, and settings.backend.region already feeds the backend
            # block. Demanding it here would raise mid-block and drop the two
            # keys that were the point of reading the file.
            if aws.get("region"):
                _setenv_if_blank("AWS_DEFAULT_REGION", aws["region"])
    except Exception:
        pass

    # Postgres state backend: OpenTofu reads PG_CONN_STR from the
    # environment. Kept out of config.tf.json deliberately — that file is
    # built by Nix into the world-readable store, so a password in the backend
    # block would be exposed to every user on every machine that builds it.
    try:
        from .config import get as _cfg_get
        pg_path = _cfg_get("backend_pg.conn_str_sops_path")
        if pg_path:
            # The sops path names its own tree — ["dbs"]["tofu-db"]… — so
            # route on that rather than assuming the integrations file. A
            # connection string lives with the databases, not the provider
            # credentials, and guessing here fails SILENTLY: the extract
            # returns non-zero, the except swallows it, and tofu later reports
            # a missing backend credential with no hint as to why.
            result = subprocess.run(
                [sops, "-d", "--extract", pg_path, _sops_file_for(pg_path, secrets_file)],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                _setenv_if_blank("PG_CONN_STR", result.stdout.strip())
    except Exception:
        pass

    # Proxmox provider ENDPOINT from the manifest. Not a credential:
    # `fleet.providers.proxmox.<inst>.endpoint` is a LAN URL the fleet
    # already declares, so it rides in the catalog rather than SOPS. It has
    # to be read before the credential block below, whose blanket
    # `PROXMOX_VE_INSECURE=true` fallback would otherwise win over a
    # manifest that says otherwise.
    #
    # Without this, a fleet whose PVE credential is an api_token files only
    # that token under integrations.proxmox — and every `fleet pve` verb
    # died on "PROXMOX_VE_ENDPOINT not set" with the endpoint sitting in
    # the manifest two directories away.
    try:
        from .config import get as _cfg_get
        instances = _cfg_get("providers.proxmox") or {}
        # First instance in sorted order, matching the credential loop below
        # so the endpoint and the token cannot come from different clusters.
        for _name in sorted(instances):
            inst = instances[_name] or {}
            if inst.get("endpoint"):
                _setenv_if_blank("PROXMOX_VE_ENDPOINT", inst["endpoint"])
                if inst.get("insecure") is not None:
                    _setenv_if_blank(
                        "PROXMOX_VE_INSECURE",
                        "true" if inst["insecure"] else "false")
                break
    except Exception:
        pass

    # Proxmox provider credentials for terranix.
    try:
        result = subprocess.run(
            [sops, "-d", "--extract", '["integrations"]["proxmox"]', secrets_file],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            import yaml
            pve = yaml.safe_load(result.stdout)
            mapping = {
                "endpoint": "PROXMOX_VE_ENDPOINT",
                "username": "PROXMOX_VE_USERNAME",
                "password": "PROXMOX_VE_PASSWORD",
                # user@realm!token=uuid form — bpg's token auth; preferred
                # over username/password where both are present.
                "api_token": "PROXMOX_VE_API_TOKEN",
            }

            # Two shapes live under this tree. A single-instance fleet files
            # the credentials flat (integrations.proxmox.api_token); a fleet
            # using named provider instances files them per instance
            # (integrations.proxmox.main.api_token), matching the
            # `fleet.providers.proxmox.<inst>` schema and the sops paths its
            # provider block already declares.
            #
            # Only the flat shape was read, so on a per-instance fleet this
            # loop matched nothing, set nothing, and `except: pass` swallowed
            # it. Terranix still worked (it resolves its own sops paths at
            # apply time), which is what made the gap invisible — but every
            # `fleet pve` verb reported "PROXMOX_VE_ENDPOINT not set" with a
            # fully populated secrets file sitting right there.
            #
            # Flat wins where both exist; otherwise take the first instance in
            # sorted order, so the choice is at least deterministic. A fleet
            # with several PVE clusters needs a real --instance selector, not
            # a guess — but a guess beats today's silent nothing.
            candidates = [pve] if isinstance(pve, dict) else []
            if isinstance(pve, dict) and not (set(mapping) & set(pve)):
                candidates = [pve[k] for k in sorted(pve)
                              if isinstance(pve[k], dict)]

            for source in candidates:
                for key, env_var in mapping.items():
                    if key in source:
                        _setenv_if_blank(env_var, source[key])
        _setenv_if_blank("PROXMOX_VE_INSECURE", "true")
    except Exception:
        pass

    # XCP-ng / XOA credentials — REST API for VM IP discovery during
    # `fleet inventory generate` (XCP-ng VMs DHCP their NICs, so static IPs
    # aren't declarable in the fleet manifest; we patch them in from live XOA).
    try:
        result = subprocess.run(
            [sops, "-d", "--extract", '["integrations"]["xen-orchestra"]', secrets_file],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            import yaml
            xoa = yaml.safe_load(result.stdout)
            # The SOPS-declared URL is wss://... (websocket) for the
            # Terraform provider. The REST API lives at https:// on the
            # same host, so convert here.
            ws_url = str(xoa.get("url", ""))
            if ws_url.startswith("wss://"):
                rest_url = "https://" + ws_url[len("wss://"):]
            elif ws_url.startswith("ws://"):
                rest_url = "http://" + ws_url[len("ws://"):]
            else:
                rest_url = ws_url
            rest_url = rest_url.rstrip("/")
            _setenv_if_blank("XOA_URL", rest_url)
            if "token" in xoa:
                _setenv_if_blank("XOA_TOKEN", xoa["token"])
        _setenv_if_blank("XOA_INSECURE", "true")
    except Exception:
        pass

    # Cloudflare API token — read-only zone/record lookups for the `tf adopt`
    # cloudflare_record resolver (INFRA-274). The terraform provider reads the
    # same secret via data.sops_file at apply time; this is the pre-apply path
    # a Python resolver needs.
    try:
        result = subprocess.run(
            [sops, "-d", "--extract",
             '["integrations"]["cloudflare"]["api_token"]', secrets_file],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            _setenv_if_blank("CLOUDFLARE_API_TOKEN", result.stdout.strip())
    except Exception:
        pass

    # Grafana Cloud tokens — read-only folder/alerting + Synthetic-Monitoring
    # lookups for the `tf adopt` grafana_* resolvers (INFRA-274). URLs are
    # literals in the provider block (surfaced by the adopt path itself); only
    # the tokens are secret. The provider reads the same secrets at apply time.
    for sops_key, env_var in (
        ('["integrations"]["grafana_cloud"]["service_account"]["token"]',
         "GRAFANA_AUTH"),
        ('["integrations"]["grafana_cloud"]["sm"]["access_token"]',
         "GRAFANA_SM_TOKEN"),
    ):
        try:
            result = subprocess.run(
                [sops, "-d", "--extract", sops_key, secrets_file],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                _setenv_if_blank(env_var, result.stdout.strip())
        except Exception:
            pass


def _maybe_reexec_for_missing_tools() -> None:
    """Re-exec fleet inside `nix develop` when required external tools are absent.

    The devshell provides colmena/tofu but not `fleet` (venv editable
    install); the venv provides `fleet` but not colmena/tofu. Non-interactive
    contexts (CI, agents, tmux relaunch) routinely get one half only. When a
    deploy-family command is invoked and its wrapped tool is missing from
    PATH, transparently re-exec this same fleet binary through `nix develop`
    so both halves are present.

    Opt-out: FLEET_NO_REEXEC=1. Loop guard: FLEET_REEXECED=1.
    """
    import os
    import shutil
    import sys

    from ._util import env_get
    if env_get("FLEET_NO_REEXEC") == "1" or env_get("FLEET_REEXECED") == "1":
        return

    argv = sys.argv[1:]
    if not argv or argv[0] != "deploy":
        return
    sub = argv[1] if len(argv) > 1 else ""
    needs = {"nixos": ["colmena"], "tf": ["tofu"]}.get(sub, ["colmena", "tofu"])
    missing = [t for t in needs if shutil.which(t) is None]
    if not missing:
        return
    if shutil.which("nix") is None:
        sys.stderr.write(
            f"fleet: required tool(s) missing from PATH ({', '.join(missing)}) "
            f"and `nix` unavailable to re-enter the devshell — this will fail.\n")
        return

    from ._util import find_project_root, fleet_executable
    root = find_project_root()
    sys.stderr.write(
        f"fleet: {', '.join(missing)} not on PATH — re-entering devshell "
        f"(nix develop {root})…\n")
    os.environ["FLEET_REEXECED"] = "1"
    os.execvp("nix", ["nix", "develop", str(root), "--command",
                      fleet_executable(), *argv])


def _dump_verbs_and_exit() -> None:
    """Print the framework CLI verb surface as sorted JSON, then return.

    Walks the composed `fleet` group (framework groups only — consumer
    extensions load AFTER this in main()). The cli-verbs-golden check diffs
    this against a committed golden, so a renamed/removed/added verb is caught.
    Deliberately touches no env, SOPS, re-exec, or extensions — safe to run in
    the Nix sandbox.
    """
    import json

    paths: list[str] = []

    def walk(group: click.Group, prefix: str) -> None:
        ctx = click.Context(group)
        for name in group.list_commands(ctx):
            sub = group.get_command(ctx, name)
            path = f"{prefix}{name}"
            paths.append(path)
            if isinstance(sub, click.Group):  # Group, not MultiCommand (removed in Click 9)
                walk(sub, f"{path} ")

    walk(fleet, "")
    print(json.dumps(sorted(paths), indent=2))


def main() -> None:
    # Fast path: dump the (framework) CLI verb surface with no env, SOPS,
    # re-exec, or consumer extensions — used by the cli-verbs-golden check in
    # the Nix sandbox. Must run before _setup_env / load_extensions so the
    # golden captures the framework surface only.
    import sys
    if "--dump-verbs" in sys.argv[1:]:
        _dump_verbs_and_exit()
        return
    _maybe_reexec_for_missing_tools()
    _setup_env()
    # Consumer command groups from the repo's cli-ext/ (fleet.settings.cli.extensionsDir).
    from .config import load_extensions
    load_extensions(fleet)
    fleet()


if __name__ == "__main__":
    main()
