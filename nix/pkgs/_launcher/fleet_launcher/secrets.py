"""fleet secrets — SOPS secrets management.

Commands:
    edit              Open secrets in $EDITOR (standard sops workflow)
    edit --plaintext  Decrypt to plaintext YAML for IDE editing, re-encrypt on confirm
    keys list         List all secret key paths
    keys get          Print one key's value (bare stdout, pipeable)
    keys add          Add or update a key
    keys rm           Remove a key
    keys replace      Replace an existing key's value
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import click
import yaml
from rich.console import Console
from rich.table import Table

console = Console()
# Diagnostics that must not land on stdout — `keys get` is designed for
# command substitution, so anything but the value itself goes to stderr.
err_console = Console(stderr=True)

DEFAULT_SECRETS_FILE = "nix/secrets/secrets.yaml"
PLAINTEXT_FILE = "nix/secrets/secrets.plaintext.yaml"


def _find_secrets_file() -> str:
    """Locate secrets.yaml relative to the repo root."""
    path = os.path.abspath(DEFAULT_SECRETS_FILE)
    if os.path.exists(path):
        return path
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        candidate = os.path.join(root, DEFAULT_SECRETS_FILE)
        if os.path.exists(candidate):
            return candidate
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return path


def _file_for_key(key_path: str, override: str | None) -> str:
    """Resolve which SOPS file owns `key_path`.

    An explicit --file wins. Otherwise the leading segment of the key path is
    a TREE, and `fleet.settings.sopsFiles` declares which file owns it —
    `config.file_for` is the same router the rest of the launcher uses, so the
    CLI and the terranix layer cannot hold different opinions about layout.

    Routing here rather than at each call site is deliberate. Every caller that
    forgot `--file` silently wrote to secrets.yaml, which in a split store is
    the one file the terranix layer never reads: it reads
    `fleet.settings.tfSopsFile`. `pve cluster issue-tf-token` did exactly that,
    stranding a freshly minted provider token where no `tofu plan` would find
    it, and leaving a live Administrator credential in the wrong file. Worse,
    a split store keeps stale partial copies of these trees in secrets.yaml,
    so the failure can surface as a stale value rather than a missing key.

    Falls back to the default file when there is no catalog to consult — a
    fleet that has not been generated yet is the single-file case by
    definition.
    """
    if override:
        return override
    from .config import FleetConfigError, file_for
    try:
        return str(file_for(key_path.split("/", 1)[0]))
    except FleetConfigError:
        return _find_secrets_file()


def _find_repo_root() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return os.getcwd()


def _require_sops() -> str:
    """Return sops binary path or exit with error."""
    sops = shutil.which("sops")
    if not sops:
        console.print("[red]ERROR:[/red] sops not found — run inside nix develop")
        sys.exit(1)
    return sops


def _ensure_age_key():
    """Ensure SOPS_AGE_KEY is set, loading from ~/.ssh/sops-age.key if needed."""
    if os.environ.get("SOPS_AGE_KEY"):
        return
    key_file = os.path.expanduser("~/.ssh/sops-age.key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            os.environ["SOPS_AGE_KEY"] = f.read().strip()
    else:
        console.print("[yellow]Warning:[/yellow] SOPS_AGE_KEY not set and ~/.ssh/sops-age.key not found")


def _sops_decrypt_to_string(secrets_file: str) -> str:
    """Decrypt secrets file to a YAML string."""
    sops = _require_sops()
    _ensure_age_key()
    result = subprocess.run(
        [sops, "-d", secrets_file],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]ERROR:[/red] sops decrypt failed: {result.stderr.strip()}")
        sys.exit(1)
    return result.stdout


def _sops_create(sops: str, secrets_file: str, parts: list[str], value: str):
    """Mint a new encrypted secrets file holding exactly one key.

    `sops --set` edits in place and fails on a path that does not exist, so a
    fleet that routes a tree to its own file (fleet.settings.sopsFiles) had no
    way to create that file through the CLI — the first key had to be written
    with a hand-rolled `sops -e` pipeline, which is exactly the bypass the
    CLI exists to remove.

    Encrypting needs only the recipients .sops.yaml names for this path, so
    this half works even where `--set` could not: no private key is read.
    """
    os.makedirs(os.path.dirname(secrets_file) or ".", exist_ok=True)
    tree: dict = {}
    node = tree
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    # --filename-override makes sops pick the format and the creation_rule
    # from the DESTINATION path rather than from /dev/stdin.
    result = subprocess.run(
        [sops, "-e", "--filename-override", secrets_file, "/dev/stdin"],
        input=yaml.safe_dump(tree, default_flow_style=False),
        capture_output=True, text=True, cwd=_find_repo_root(),
    )
    if result.returncode != 0:
        console.print(f"[red]ERROR:[/red] sops encrypt failed: {result.stderr.strip()}")
        sys.exit(1)
    with open(secrets_file, "w") as fh:
        fh.write(result.stdout)
    console.print(f"[green]created[/green] {secrets_file} (encrypted)")


def _sops_set(secrets_file: str, key_path: str, value: str):
    """Set a key in the secrets file using sops --set."""
    sops = _require_sops()
    _ensure_age_key()
    if not os.path.exists(secrets_file):
        _sops_create(sops, secrets_file, key_path.split("/"), value)
        return
    # sops --set expects a Python-literal-ish expression where the value is
    # parsed as JSON, e.g. '["key1"]["key2"] "value"'. Hand-quoting via
    # f-string corrupts any value containing newlines, double quotes, or
    # backslashes (e.g. PEM cert chains). json.dumps gives a properly
    # escaped JSON string literal that sops accepts byte-for-byte.
    parts = key_path.split("/")
    sops_path = "".join(f'["{p}"]' for p in parts)
    sops_expr = f'{sops_path} {json.dumps(value)}'
    result = subprocess.run(
        [sops, "--set", sops_expr, secrets_file],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]ERROR:[/red] sops set failed: {result.stderr.strip()}")
        sys.exit(1)


def _sops_rm(secrets_file: str, key_path: str):
    """Remove a key from the secrets file using sops --set to null then cleanup."""
    # sops doesn't have a native --rm, so we decrypt, remove the key, re-encrypt
    sops = _require_sops()
    _ensure_age_key()

    plaintext = _sops_decrypt_to_string(secrets_file)
    data = yaml.safe_load(plaintext)

    # Navigate and remove the key
    parts = key_path.split("/")
    parent = data
    for part in parts[:-1]:
        if isinstance(parent, dict) and part in parent:
            parent = parent[part]
        else:
            console.print(f"[red]ERROR:[/red] Key path not found: {key_path}")
            sys.exit(1)

    if parts[-1] not in parent:
        console.print(f"[red]ERROR:[/red] Key not found: {key_path}")
        sys.exit(1)

    del parent[parts[-1]]

    # Clean up empty parent dicts
    # (don't bother, sops handles it)

    # Write back and re-encrypt. The temp file MUST live in the same
    # directory as secrets.yaml with a .yaml suffix so the repo's
    # .sops.yaml creation_rules match it — sops fails config matching
    # ("no matching creation rules") for a /tmp path before it even
    # considers explicit --age recipients (INFRA-197 fix; same pattern
    # as the `edit --plaintext` re-encrypt below). Matching the real
    # creation rule also re-encrypts for the CORRECT full recipient set
    # instead of whatever we could scrape from the old file's metadata.
    sf_dir = os.path.dirname(secrets_file) or "."
    with tempfile.NamedTemporaryFile(
        mode="w", dir=sf_dir, suffix=".yaml", delete=False
    ) as f:
        yaml.dump(data, f, default_flow_style=False)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sops, "-e", "-i", tmp_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]ERROR:[/red] re-encrypt failed: {result.stderr.strip()}")
            sys.exit(1)
        shutil.move(tmp_path, secrets_file)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _list_keys(data: dict, prefix: str = "") -> list[str]:
    """Recursively list all leaf key paths in a dict."""
    keys = []
    for k, v in sorted(data.items()):
        if k == "sops":  # Skip sops metadata
            continue
        path = f"{prefix}/{k}" if prefix else k
        if isinstance(v, dict):
            keys.extend(_list_keys(v, path))
        else:
            keys.append(path)
    return keys


def _lookup(secrets_file: str, key_path: str):
    """Resolve a slash-separated key path to its node in the decrypted tree.

    Exits 1 (message on stderr, so stdout stays pipeable) if the path does
    not resolve. Returns the node as-is — callers decide whether a dict is
    acceptable; `get` rejects it, `replace` only cares that it exists.
    """
    _ensure_age_key()
    data = yaml.safe_load(_sops_decrypt_to_string(secrets_file))

    node = data
    for part in key_path.split("/"):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            err_console.print(f"[red]ERROR:[/red] Key not found: {key_path}")
            sys.exit(1)
    return node


# ─────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────

@click.group()
def secrets():
    """Manage SOPS-encrypted secrets.

    Operates on the fleet's SOPS store — ``nix/secrets/secrets.yaml`` by
    default, or whichever file ``fleet.settings.sopsFiles`` routes a key's
    top-level tree to. Requires ``sops`` and the SOPS age key (set via
    SOPS_AGE_KEY or ~/.ssh/sops-age.key).
    """


@secrets.command("init")
@click.option("--age-key-file", default=None,
              help="Age key location (default: $FLEET_AGE_KEY_FILE, "
                   "falling back to ~/.ssh/sops-age.key).")
@click.option("--from-ssh-key", default=None, type=click.Path(exists=True),
              help="Derive the age key from an existing ed25519 SSH private key "
                   "(ssh-to-age) instead of generating a fresh one.")
def secrets_init(age_key_file: str | None, from_ssh_key: str | None):
    """Scaffold this fleet's SOPS store (idempotent).

    Creates everything a fleet needs for secrets to Just Work:

    \b
      1. an age keypair (generated, or derived from an SSH key)
      2. .sops.yaml at the repo root with the key as recipient
      3. an encrypted nix/secrets/secrets.yaml stub

    The store path is fleetkit convention (nix/secrets/secrets.yaml,
    overridable via fleet.settings.sopsSecretsFile). Pass the same path
    to mkFleet's `secretsFile` argument so hosts decrypt it at
    activation. Existing files are left untouched.
    """
    from .config import repo_root as _repo_root
    from .config import age_key_file as _cfg_age_key

    root = str(_repo_root())
    key_path = os.path.expanduser(age_key_file or _cfg_age_key())
    secrets_path = _find_secrets_file()

    # 1. Age key.
    if os.path.exists(key_path):
        console.print(f"age key exists: {key_path}")
    else:
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        if from_ssh_key:
            out = subprocess.run(["ssh-to-age", "-private-key", "-i", from_ssh_key],
                                 capture_output=True, text=True, check=True)
            with open(key_path, "w") as f:
                f.write(out.stdout)
        else:
            subprocess.run(["age-keygen", "-o", key_path], check=True)
        os.chmod(key_path, 0o600)
        console.print(f"[green]created[/green] age key: {key_path}")

    pub = subprocess.run(["age-keygen", "-y", key_path],
                         capture_output=True, text=True, check=True).stdout.strip()
    console.print(f"recipient: {pub}")

    # 2. .sops.yaml recipient config.
    sops_yaml = os.path.join(root, ".sops.yaml")
    if os.path.exists(sops_yaml):
        console.print(f".sops.yaml exists: {sops_yaml} — add the recipient yourself if it's new")
    else:
        rel = os.path.relpath(secrets_path, root)
        with open(sops_yaml, "w") as f:
            f.write("# sops recipient config — scaffolded by `fleet secrets init`.\n"
                    "keys:\n"
                    f"  - &fleet {pub}\n"
                    "creation_rules:\n"
                    f"  - path_regex: {os.path.dirname(rel)}/.*\\.yaml$\n"
                    "    key_groups:\n"
                    "      - age:\n"
                    "          - *fleet\n")
        console.print(f"[green]created[/green] {sops_yaml}")

    # 3. Encrypted secrets stub.
    if os.path.exists(secrets_path):
        console.print(f"secrets store exists: {secrets_path}")
    else:
        os.makedirs(os.path.dirname(secrets_path), exist_ok=True)
        stub = os.path.join(os.path.dirname(secrets_path), ".stub.yaml")
        with open(stub, "w") as f:
            f.write("# fleet secrets — edit with `fleet secrets edit`.\n"
                    "integrations: {}\n"
                    "services: {}\n")
        env = dict(os.environ, SOPS_AGE_KEY_FILE=key_path)
        with open(secrets_path, "w") as out_f:
            subprocess.run(["sops", "--config", sops_yaml, "-e", stub],
                           check=True, stdout=out_f, env=env, cwd=root)
        os.unlink(stub)
        console.print(f"[green]created[/green] {secrets_path} (encrypted)")

    console.print("\nwire it into your flake:  mkFleet { …; secretsFile = ./"
                  + os.path.relpath(secrets_path, root) + "; }", style="bold")


@secrets.command("edit")
@click.option("--file", "secrets_file", default=None,
              help="Override path to secrets.yaml.")
@click.option("--plaintext", is_flag=True,
              help="Decrypt to plaintext YAML for IDE editing.")
def secrets_edit(secrets_file: str | None, plaintext: bool):
    """Edit secrets in your terminal editor or IDE.

    Without --plaintext: standard sops workflow (decrypt → $EDITOR → re-encrypt).
    With --plaintext: decrypt to secrets.plaintext.yaml, edit in your IDE,
    then re-encrypt when you confirm.
    """
    sf = secrets_file or _find_secrets_file()
    sops = _require_sops()

    if not plaintext:
        _ensure_age_key()
        os.execvp(sops, [sops, sf])
    else:
        _ensure_age_key()
        root = _find_repo_root()
        pt_path = os.path.join(root, PLAINTEXT_FILE)

        # Decrypt to plaintext
        content = _sops_decrypt_to_string(sf)
        with open(pt_path, "w") as f:
            f.write(content)
        console.print(f"[green]Decrypted to:[/green] {pt_path}")
        console.print("[yellow]Edit this file in your IDE, then come back here.[/yellow]")
        console.print("")

        if click.confirm("Re-encrypt and save?", default=True):
            # Encrypt to a temp file FIRST; only replace secrets.yaml atomically
            # once sops succeeds. This keeps the original encrypted file intact
            # if re-encrypt fails (bad YAML, duplicate keys, missing age key, etc.).
            #
            # Temp file must:
            #   - end in .yaml so the `.sops.yaml` creation_rules regex matches
            #   - live in the same directory so relative .sops.yaml config applies
            sf_dir = os.path.dirname(sf) or "."
            with tempfile.NamedTemporaryFile(
                mode="w", dir=sf_dir, suffix=".yaml", delete=False
            ) as tf:
                with open(pt_path) as src:
                    shutil.copyfileobj(src, tf)
                tmp_path = tf.name
            try:
                result = subprocess.run(
                    [sops, "-e", "-i", tmp_path],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    console.print(f"[red]ERROR:[/red] re-encrypt failed: {result.stderr.strip()}")
                    console.print(f"[yellow]Plaintext file preserved at:[/yellow] {pt_path}")
                    console.print("[dim]secrets.yaml was NOT modified.[/dim]")
                    sys.exit(1)
                # Success — atomically replace secrets.yaml
                os.replace(tmp_path, sf)
                tmp_path = None  # prevent unlink in finally
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            console.print("[green]Secrets re-encrypted.[/green]")
        else:
            console.print("[dim]Aborted. Plaintext file preserved.[/dim]")
            return

        # Clean up plaintext
        os.unlink(pt_path)
        console.print(f"[dim]Removed {pt_path}[/dim]")


@secrets.command("host-recipient")
@click.argument("host")
def secrets_host_recipient(host: str):
    """Print the age recipient for HOST, from its live SSH host key.

    sops-nix decrypts on a node using that node's own ed25519 SSH host
    key, so adding a host to a creation rule means putting the age
    translation of its PUBLIC host key in .sops.yaml. That is a
    keyscan piped through ssh-to-age — a step that, done by hand once
    per new container, is exactly the kind of thing that gets written
    into a comment and then mistyped.

    HOST is an IP or hostname. Prints one `age1…` line, nothing else,
    so it can be pasted straight under `keys:` as a new anchor.
    """
    for tool in ("ssh-keyscan", "ssh-to-age"):
        if not shutil.which(tool):
            console.print(f"[red]ERROR:[/red] {tool} not found — run inside nix develop")
            sys.exit(1)

    scan = subprocess.run(["ssh-keyscan", "-t", "ed25519", host],
                          capture_output=True, text=True, timeout=30)
    # ssh-keyscan reports "no route", "connection refused" and "no key of
    # that type" alike on stderr and still exits 0, so the empty-stdout
    # case has to be caught here or the operator gets an ssh-to-age parse
    # error instead of the real reason.
    lines = [l for l in scan.stdout.splitlines() if l and not l.startswith("#")]
    if not lines:
        console.print(f"[red]ERROR:[/red] no ed25519 host key from {host} — "
                      f"is it up and running sshd?")
        if scan.stderr.strip():
            console.print(f"[dim]{scan.stderr.strip()}[/dim]")
        sys.exit(1)

    # "<host> ssh-ed25519 AAAA…" → the key itself.
    pub = " ".join(lines[0].split()[1:])
    conv = subprocess.run(["ssh-to-age"], input=pub + "\n",
                          capture_output=True, text=True)
    if conv.returncode != 0 or not conv.stdout.strip():
        console.print(f"[red]ERROR:[/red] ssh-to-age failed: {conv.stderr.strip()}")
        sys.exit(1)
    click.echo(conv.stdout.strip())


@secrets.command("updatekeys")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--yes", "-y", is_flag=True, help="Don't prompt per file.")
def secrets_updatekeys(files: tuple[str, ...], yes: bool):
    """Re-encrypt FILES to the recipients .sops.yaml now names.

    Editing a creation rule changes nothing on its own — an existing
    file keeps the recipient list it was encrypted with, so a host
    added to .sops.yaml still cannot decrypt until this runs. Wraps
    `sops updatekeys` only to supply the fleet's age key the same way
    every other verb in this group does; without it the operator is
    exporting SOPS_AGE_KEY_FILE by hand at the exact moment they are
    changing who can read a secret.
    """
    sops = _require_sops()
    _ensure_age_key()
    for path in files:
        cmd = [sops, "updatekeys"] + (["-y"] if yes else []) + [path]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            console.print(f"[red]ERROR:[/red] sops updatekeys failed for {path}")
            sys.exit(result.returncode)
        console.print(f"[green]updated[/green] {path}")


@secrets.group("keys")
def keys():
    """Manage individual secret keys."""


@keys.command("list")
@click.option("--file", "secrets_file", default=None)
def keys_list(secrets_file: str | None):
    """List all secret key paths."""
    sf = secrets_file or _find_secrets_file()
    _ensure_age_key()
    plaintext = _sops_decrypt_to_string(sf)
    data = yaml.safe_load(plaintext)

    key_paths = _list_keys(data)
    console.print(f"[bold]{len(key_paths)} keys:[/bold]")
    for path in key_paths:
        console.print(f"  {path}")


@keys.command("get")
@click.argument("key_path")
@click.option("--file", "secrets_file", default=None)
@click.option("-n", "--no-newline", is_flag=True,
              help="Omit the trailing newline (for piping into tools that mind).")
def keys_get(key_path: str, secrets_file: str | None, no_newline: bool):
    """Print one secret key's value.

    KEY_PATH is slash-separated (e.g., services/grafana/admin_password).

    The value goes to stdout bare and unformatted, so this is safe in command
    substitution — it replaces raw `sops -d --extract '["a"]["b"]' file.yaml`:

        export JIRA_API_TOKEN=$(fleet devtools secrets keys get \\
            integrations/jira/api_token)

    The file is chosen from the key's top-level tree via
    fleet.settings.sopsFiles; pass --file only to override that.
    """
    sf = _file_for_key(key_path, secrets_file)
    node = _lookup(sf, key_path)

    if isinstance(node, dict):
        # A branch, not a leaf. Naming the children beats printing a dict
        # repr the caller would have to parse.
        err_console.print(
            f"[red]ERROR:[/red] '{key_path}' is a group, not a value. Contains:")
        for child in _list_keys(node, key_path):
            err_console.print(f"  {child}")
        sys.exit(1)

    click.echo(node, nl=not no_newline)


@keys.command("add")
@click.argument("key_path")
@click.argument("value")
@click.option("--file", "secrets_file", default=None,
              help="Override the file the key's tree routes to. "
                   "Created, encrypted per .sops.yaml, if it does not exist.")
def keys_add(key_path: str, value: str, secrets_file: str | None):
    """Add or set a secret key.

    KEY_PATH is slash-separated (e.g., services/grafana/admin_password).
    VALUE is the plaintext secret value.

    The target file comes from the key's top-level tree via
    fleet.settings.sopsFiles, so `integrations/...` lands in the file the
    terranix layer actually reads. Writing into a file that does not exist
    yet creates it — that is how a routed tree gets its first key.
    """
    sf = _file_for_key(key_path, secrets_file)
    _sops_set(sf, key_path, value)
    console.print(f"[green]Set:[/green] {key_path}")


@keys.command("rm")
@click.argument("key_path")
@click.option("--file", "secrets_file", default=None)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def keys_rm(key_path: str, secrets_file: str | None, yes: bool):
    """Remove a secret key.

    KEY_PATH is slash-separated (e.g., services/grafana/admin_password).
    """
    sf = _file_for_key(key_path, secrets_file)
    if not yes:
        click.confirm(f"Remove key '{key_path}' from {sf}?", abort=True)
    _sops_rm(sf, key_path)
    console.print(f"[green]Removed:[/green] {key_path}")


@keys.command("replace")
@click.argument("key_path")
@click.argument("new_value")
@click.option("--file", "secrets_file", default=None)
def keys_replace(key_path: str, new_value: str, secrets_file: str | None):
    """Replace an existing secret key's value.

    KEY_PATH is slash-separated (e.g., services/grafana/admin_password).
    NEW_VALUE is the new plaintext value.
    """
    sf = _file_for_key(key_path, secrets_file)

    _lookup(sf, key_path)  # replace, not create — fail if it isn't already there
    _sops_set(sf, key_path, new_value)
    console.print(f"[green]Replaced:[/green] {key_path}")


# ── env-export (ADR-096 A5) ──────────────────────────────────────────
# Materialise per-instance .env files from the fleet.secrets catalog:
# connection facts DERIVED (host address from hosts.json via the
# instance's first host consumer), values decrypted per-key straight out
# of the resource's own sops file — or left as sops:// references with
# --no-values, which is the shareable/CI-safe template form. Output under
# .cache/fleet/env/<resource>/<instance>.env, mode 0600.

def _load_secrets_catalog(root: str) -> dict:
    import json as _json
    out = subprocess.run(
        ["nix", "build", ".#secrets-catalog-json", "--no-link", "--print-out-paths"],
        capture_output=True, text=True, cwd=root)
    if out.returncode != 0:
        raise click.ClickException(
            "building .#secrets-catalog-json failed — is this consumer on a "
            f"fleetkit with the secrets catalog?\n{out.stderr.strip()[-400:]}")
    with open(out.stdout.strip().splitlines()[-1]) as f:
        return _json.load(f)


def _hosts_json(root: str) -> dict:
    import json as _json
    from ._util import fleet_cache_dir
    from pathlib import Path
    hj = fleet_cache_dir(Path(root)) / "hosts.json"
    if hj.exists():
        with open(hj) as f:
            return _json.load(f)
    return {}


def _env_name(prefix: str, leaf: str) -> str:
    return f"{prefix}_{leaf}".upper().replace("-", "_").replace("/", "_")


@secrets.command("env-export")
@click.argument("selector", required=False)
@click.option("--all", "export_all", is_flag=True, help="Export every resource/instance in the catalog.")
@click.option("--no-values", is_flag=True,
              help="Emit sops://<resource>/<instance>/<name> references instead of decrypted values — safe to share or commit as a template.")
@click.option("--out", "out_dir", default=None,
              help="Output directory (default: <repo>/.cache/fleet/env).")
def secrets_env_export(selector: str | None, export_all: bool, no_values: bool, out_dir: str | None):
    """Write .env files for RESOURCE or RESOURCE/INSTANCE from fleet.secrets.

    A credential without its address is useless to the developer consuming
    it, so each file carries <PREFIX>_HOST derived from the instance's
    first host consumer, any declared vars, and one variable per secret.
    """
    import json as _json
    import os
    import stat
    from pathlib import Path

    if not selector and not export_all:
        raise click.UsageError("name a RESOURCE[/INSTANCE] or pass --all")
    root = _find_repo_root()
    catalog = _load_secrets_catalog(root)
    hosts = _hosts_json(root)
    base = Path(out_dir) if out_dir else Path(root) / ".cache" / "fleet" / "env"

    want_res, want_inst = (None, None)
    if selector:
        parts = selector.split("/", 1)
        want_res = parts[0]
        want_inst = parts[1] if len(parts) > 1 else None
        if want_res not in catalog:
            raise click.ClickException(
                f"no such secret resource '{want_res}' "
                f"(catalog has: {', '.join(sorted(catalog)) or 'nothing'})")

    if not no_values:
        _require_sops()
        _ensure_age_key()

    written = 0
    for rname, res in sorted(catalog.items()):
        if want_res and rname != want_res:
            continue
        sfile = res.get("file") or ""
        for iname, inst in sorted((res.get("instances") or {}).items()):
            if want_inst and iname != want_inst:
                continue
            prefix = inst.get("envPrefix") or f"{rname}_{iname}"
            cons = inst.get("consumers") or res.get("consumers") or {}
            lines = [f"# generated by `fleet secrets env-export` — do not edit",
                     f"# resource={rname} instance={iname} file={sfile}"]
            for h in (cons.get("hosts") or [])[:1]:
                ip = (hosts.get(h) or {}).get("internal_ip") or (hosts.get(h) or {}).get("ip")
                if ip:
                    lines.append(f"{_env_name(prefix, 'HOST')}={ip}")
            for k, v in sorted((inst.get("vars") or {}).items()):
                lines.append(f"{_env_name(prefix, k)}={v}")
            for sname, sdef in sorted((inst.get("secrets") or {}).items()):
                var = _env_name(prefix, (sdef or {}).get("envVar") or sname)
                if no_values:
                    lines.append(f"{var}=sops://{rname}/{iname}/{sname}")
                else:
                    r = subprocess.run(
                        [_require_sops(), "-d", "--extract",
                         f'["{iname}"]["{sname}"]', sfile],
                        capture_output=True, text=True)
                    if r.returncode != 0:
                        raise click.ClickException(
                            f"decrypt failed for {rname}/{iname}/{sname} in {sfile}: "
                            f"{r.stderr.strip()[-200:]}")
                    lines.append(f"{var}={r.stdout.strip()}")
            dest = base / rname / f"{iname}.env"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("\n".join(lines) + "\n")
            os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
            written += 1
            console.print(f"  [green]{dest.relative_to(Path(root)) if dest.is_relative_to(Path(root)) else dest}[/green]"
                          + ("  [dim](references only)[/dim]" if no_values else ""))
    if written == 0:
        raise click.ClickException("selector matched nothing")
    console.print(f"[bold]{written}[/bold] env file(s) written under {base}")


@secrets.command("check")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def secrets_check(as_json: bool) -> None:
    """Audit SOPS routing: is every tree where the fleet says it is?

    Splitting a secrets store and leaving the original populated creates a
    failure that never announces itself. A consumer aimed at the wrong file
    ERRORS when the key is gone — but when a stale duplicate survives, the
    read SUCCEEDS and returns a diverged old value. Nothing distinguishes it
    from a correct read until the two copies disagree, which is typically a
    rotation later.

    This reads only the PLAINTEXT key names — sops encrypts values, not
    structure — so it needs no age key and decrypts nothing.
    """
    import yaml as _yaml
    from pathlib import Path
    from . import config as _config
    from ._util import find_project_root

    root = find_project_root()
    secrets_dir = root / "nix" / "secrets"
    if not secrets_dir.is_dir():
        console.print(f"[red]ERROR:[/red] {secrets_dir} does not exist")
        sys.exit(1)

    routes = _config.get("sops.files", {}) or {}
    default_file = Path(_config.get("sops.secrets_file", "nix/secrets/secrets.yaml")).name

    # `secrets edit --plaintext` leaves a DECRYPTED file behind. It is
    # gitignored, so it never reaches a remote — but it is real secret material
    # sitting on a disk, and it is not part of the routing story, so it is
    # excluded from the analysis and reported separately below.
    plaintext_files = []
    for f in sorted(secrets_dir.glob("*.plaintext.yaml")):
        mode = oct(f.stat().st_mode & 0o777)[2:]
        plaintext_files.append((f, mode, f.stat().st_size))

    # tree -> {filename: key count}
    trees: dict[str, dict[str, int]] = {}
    for f in sorted(secrets_dir.glob("*.yaml")):
        if f.name.endswith(".plaintext.yaml"):
            continue
        try:
            doc = _yaml.safe_load(f.read_text()) or {}
        except Exception as e:
            console.print(f"[yellow]skip[/yellow] {f.name}: unparseable ({e})")
            continue
        for tree, body in doc.items():
            if tree.startswith("sops"):      # sops' own metadata block
                continue
            n = len(body) if isinstance(body, dict) else 1
            trees.setdefault(tree, {})[f.name] = n

    problems: list[dict] = []
    for tree, where in sorted(trees.items()):
        owner = Path(routes[tree]).name if tree in routes else default_file
        others = sorted(k for k in where if k != owner)
        if owner not in where:
            problems.append({"tree": tree, "kind": "missing", "owner": owner,
                             "found_in": others,
                             "detail": f"routed to {owner}, which does not contain it"})
        elif others:
            problems.append({"tree": tree, "kind": "duplicate", "owner": owner,
                             "found_in": others,
                             "detail": "also in " + ", ".join(
                                 f"{o} ({where[o]} keys)" for o in others)})

    if as_json:
        console.print(json.dumps(
            {"routes": routes, "trees": trees, "problems": problems}, indent=2))
        sys.exit(1 if problems else 0)

    table = Table(show_header=True, header_style="bold")
    table.add_column("tree"); table.add_column("owner"); table.add_column("status")
    for tree, where in sorted(trees.items()):
        owner = Path(routes[tree]).name if tree in routes else default_file
        p = next((x for x in problems if x["tree"] == tree), None)
        if p is None:
            status = "[green]ok[/green]"
        elif p["kind"] == "missing":
            status = f"[red]MISSING[/red] — {p['detail']}"
        else:
            status = f"[yellow]STALE DUPLICATE[/yellow] — {p['detail']}"
        table.add_row(tree, owner + ("" if tree in routes else "  (default)"), status)
    console.print(table)

    for f, mode, size in plaintext_files:
        console.print(
            f"[red]DECRYPTED SECRETS ON DISK[/red] {f} ({size} bytes, mode {mode})\n"
            "  Left behind by `secrets edit --plaintext`. Gitignored, so it cannot reach a\n"
            "  remote — but it is plaintext fleet credentials on a local disk"
            + (", readable by every\n  user on this machine." if mode.endswith(("4", "5", "6", "7"))
               else ".")
            + " Remove it when you are done editing.")

    if not problems:
        console.print("[green]OK[/green] every tree lives in exactly the file the fleet routes it to.")
        sys.exit(1 if plaintext_files else 0)

    dupes = [p for p in problems if p["kind"] == "duplicate"]
    missing = [p for p in problems if p["kind"] == "missing"]
    if missing:
        console.print(f"[red]{len(missing)} tree(s) are routed to a file that does not hold them[/red]"
                      " — consumers of these fail loudly.")
    if dupes:
        console.print(f"[yellow]{len(dupes)} tree(s) exist in more than one file[/yellow]"
                      " — a consumer pointed at the wrong one reads a STALE value and never errors."
                      " Trim the copies out of the non-owning file.")
    sys.exit(1)
