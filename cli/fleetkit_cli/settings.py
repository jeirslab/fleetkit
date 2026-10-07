"""Runner settings: where the estate repo is, where state and work dirs go, and
the environment Pulumi and Colmena run with. Read once from the environment
(and the command line); nothing here is read from the model at runtime.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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
class Settings:
    flake: Path
    state_dir: Path
    backend_url: str
    passphrase: str
    stack: str = "main"
    secret_roots: list[Path] = field(default_factory=list)
    colmena: str = "colmena"
    nix: str = "nix"
    pulumi_home: Path | None = None

    @classmethod
    def from_env(cls, flake: str | None = None, state_dir: str | None = None) -> "Settings":
        flake_path = Path(flake or os.environ.get("FLEETKIT_FLAKE") or os.getcwd()).resolve()
        if not (flake_path / "flake.nix").is_file():
            raise SettingsError(f"{flake_path} has no flake.nix; pass --flake or set FLEETKIT_FLAKE")
        # Never Pulumi Cloud by default: without a backend the CLI falls back to
        # it (and may create an account there).
        backend = os.environ.get("PULUMI_BACKEND_URL")
        if not backend:
            raise SettingsError(
                "PULUMI_BACKEND_URL is not set; name the state backend explicitly "
                "(file://, s3://, postgres://, ...)")
        passphrase = _read_secret("PULUMI_CONFIG_PASSPHRASE")
        if passphrase is None:
            raise SettingsError(
                "PULUMI_CONFIG_PASSPHRASE (or PULUMI_CONFIG_PASSPHRASE_FILE) is not set; "
                "it encrypts the secrets Pulumi keeps in state")
        sd = Path(state_dir or os.environ.get("FLEETKIT_STATE_DIR") or flake_path / ".fleet" / "fleetkit")
        roots = [flake_path] + [Path(p) for p in os.environ.get("FLEETKIT_SECRET_ROOTS", "").split(":") if p]
        home = os.environ.get("PULUMI_HOME")
        return cls(
            flake=flake_path,
            state_dir=sd.resolve(),
            backend_url=backend,
            passphrase=passphrase,
            stack=os.environ.get("FLEETKIT_STACK", "main"),
            secret_roots=roots,
            colmena=os.environ.get("FLEETKIT_COLMENA", "colmena"),
            nix=os.environ.get("FLEETKIT_NIX", "nix"),
            pulumi_home=Path(home) if home else None,
        )

    def pulumi_env(self) -> dict[str, str]:
        env = {
            "PULUMI_BACKEND_URL": self.backend_url,
            "PULUMI_CONFIG_PASSPHRASE": self.passphrase,
            "PULUMI_SKIP_UPDATE_CHECK": "1",
        }
        for k in ("SOPS_AGE_KEY_FILE", "SOPS_AGE_KEY"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        return env
