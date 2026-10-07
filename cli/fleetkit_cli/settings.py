"""Runner settings: where the estate repo is (a working tree, or a git URL the
server keeps a mirror of), where state and work dirs go, and the environment
Pulumi and Colmena run with. Read once from the environment (and the command
line); nothing here is read from the model.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path


class SettingsError(Exception):
    pass


def _read_secret(name: str) -> str | None:
    """NAME or NAME_FILE from the environment."""
    if os.environ.get(name):
        return os.environ[name]
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).read_text().strip()
    return None


@dataclass
class GitSettings:
    url: str
    branch: str = "main"
    poll: int = 0  # seconds between fetches; 0 = only on a webhook or request
    deploy_on_push: list[str] = field(default_factory=list)  # estates
    push_mode: str = "deploy"  # or "preview": plan every push, apply on request
    webhook_secret: str | None = None
    keep: int = 5  # checkouts kept


@dataclass
class Settings:
    flake: Path | None
    state_dir: Path
    passphrase: str
    # A fallback for stacks whose Pulumi.nix names no backend.
    backend_url: str | None = None
    stack: str = "main"
    extra_secret_roots: list[Path] = field(default_factory=list)
    colmena: str = "colmena"
    nix: str = "nix"
    sops: str = "sops"
    pulumi_home: Path | None = None
    git: GitSettings | None = None

    @property
    def secret_roots(self) -> list[Path]:
        return ([self.flake] if self.flake else []) + self.extra_secret_roots

    def at(self, flake: Path) -> "Settings":
        """The same settings, on another checkout of the estate repo."""
        return replace(self, flake=flake)

    @classmethod
    def from_env(cls, flake: str | None = None, state_dir: str | None = None,
                 repo: str | None = None) -> "Settings":
        repo = repo or os.environ.get("FLEETKIT_REPO")
        flake_path: Path | None = None
        if not repo:
            flake_path = Path(flake or os.environ.get("FLEETKIT_FLAKE") or os.getcwd()).resolve()
            if not (flake_path / "flake.nix").is_file():
                raise SettingsError(f"{flake_path} has no flake.nix; pass --flake, set FLEETKIT_FLAKE "
                                    "or FLEETKIT_REPO")
        passphrase = _read_secret("PULUMI_CONFIG_PASSPHRASE")
        if passphrase is None:
            raise SettingsError(
                "PULUMI_CONFIG_PASSPHRASE (or PULUMI_CONFIG_PASSPHRASE_FILE) is not set; "
                "it encrypts the secrets Pulumi keeps in state")
        default_sd = (flake_path / ".fleet" / "fleetkit") if flake_path else Path("/var/lib/fleetkit")
        sd = Path(state_dir or os.environ.get("FLEETKIT_STATE_DIR") or default_sd)
        roots = [Path(p) for p in os.environ.get("FLEETKIT_SECRET_ROOTS", "").split(":") if p]
        home = os.environ.get("PULUMI_HOME")
        git = None
        if repo:
            git = GitSettings(
                url=repo,
                branch=os.environ.get("FLEETKIT_BRANCH", "main"),
                poll=int(os.environ.get("FLEETKIT_POLL", "0")),
                deploy_on_push=[e for e in os.environ.get("FLEETKIT_DEPLOY_ON_PUSH", "").split(",") if e],
                webhook_secret=_read_secret("FLEETKIT_WEBHOOK_SECRET"),
                push_mode=os.environ.get("FLEETKIT_PUSH_MODE", "deploy"),
            )
            if git.push_mode not in ("deploy", "preview"):
                raise SettingsError(f"FLEETKIT_PUSH_MODE is {git.push_mode!r}; deploy or preview")
        return cls(
            flake=flake_path,
            state_dir=sd.resolve(),
            passphrase=passphrase,
            backend_url=os.environ.get("PULUMI_BACKEND_URL") or None,
            stack=os.environ.get("FLEETKIT_STACK", "main"),
            extra_secret_roots=roots,
            colmena=os.environ.get("FLEETKIT_COLMENA", "colmena"),
            nix=os.environ.get("FLEETKIT_NIX", "nix"),
            sops=os.environ.get("FLEETKIT_SOPS", "sops"),
            pulumi_home=Path(home) if home else None,
            git=git,
        )

    def base_env(self) -> dict[str, str]:
        env = {
            "PULUMI_CONFIG_PASSPHRASE": self.passphrase,
            "PULUMI_SKIP_UPDATE_CHECK": "1",
        }
        for k in ("SOPS_AGE_KEY_FILE", "SOPS_AGE_KEY"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        return env
