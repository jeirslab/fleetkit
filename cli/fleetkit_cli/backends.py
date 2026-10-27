"""A stack's state backend, from what Pulumi.nix says (lib/pulumi/backend.nix),
as the environment Pulumi runs with. Secrets (a postgres connection string,
S3 keys) are decrypted here with sops and registered with the emitter, so a
tool that prints them (Pulumi does print a backend URL it cannot open) shows
[secret] instead.

  postgres   PULUMI_BACKEND_URL = the decrypted connection string (+ urlParams)
  s3         PULUMI_BACKEND_URL = url; AWS_* from sops
  local      PULUMI_BACKEND_URL = file://<state dir>/<path>
  none       PULUMI_BACKEND_URL from the runner's environment, or an error:
             never Pulumi Cloud by default.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .events import Emitter
from .render import RenderError, find_secret
from .settings import Settings


def _sops(s: Settings, v: dict[str, Any], ev: Emitter, st: dict[str, Any] | None = None) -> str:
    path = find_secret(s, v["path"], st)
    extract = "".join(f"[{json.dumps(k)}]" for k in v["extract"])
    env = s.base_env()
    p = subprocess.run([s.sops, "--decrypt", "--extract", extract, str(path)],
                       capture_output=True, text=True, env={**os.environ, **env})
    if p.returncode != 0:
        raise RenderError(f"sops could not decrypt {v['path']} {extract}: {p.stderr.strip()[-500:]}")
    return ev.secret(p.stdout.strip())


_TMP: list[str] = []


def _cleanup() -> None:
    while _TMP:
        shutil.rmtree(_TMP.pop(), ignore_errors=True)


atexit.register(_cleanup)


def _passphrase_file(value: str) -> str:
    """The passphrase in a 0600 file of a 0700 directory (on the runtime tmpfs
    when there is one), removed when the process ends. Only the path goes into
    the environment of Pulumi."""
    d = tempfile.mkdtemp(prefix="fleetkit-pp-", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
    _TMP.append(d)
    f = os.path.join(d, "passphrase")
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    return f


def _with_params(url: str, params: dict[str, str]) -> str:
    if not params:
        return url
    u = urlsplit(url)
    q = dict(parse_qsl(u.query)) | params
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def env_for(s: Settings, stack: str, backend: dict[str, Any] | None, ev: Emitter,
            st: dict[str, Any] | None = None) -> dict[str, str]:
    """`st`: the stack as evaluated, for the secret roots its flake declares."""
    env = s.base_env()
    if s.passphrase is not None:
        ev.secret(s.passphrase)
    elif backend is not None and backend.get("passphraseSecret"):
        # No passphrase in the runner's environment: the one the model declares
        # (fleet.backends.<b>.passphraseRef), decrypted now into a temporary file.
        env["PULUMI_CONFIG_PASSPHRASE_FILE"] = _passphrase_file(_sops(s, backend["passphraseSecret"], ev, st))
    else:
        raise RenderError(
            f"stack {stack}: no Pulumi passphrase. Set PULUMI_CONFIG_PASSPHRASE (or _FILE), or declare "
            "passphraseRef on the stack's backend (fleet.backends.<name>.passphraseRef)")
    if backend is None:
        if not s.backend_url:
            raise RenderError(f"stack {stack} names no backend and PULUMI_BACKEND_URL is not set; "
                              "never Pulumi Cloud by default")
        env["PULUMI_BACKEND_URL"] = s.backend_url
        ev.emit("infra", "backend", stack=stack, type="environment")
        return env
    t = backend["type"]
    if t == "local":
        d = s.state_dir / (backend.get("path") or "pulumi-state")
        d.mkdir(parents=True, exist_ok=True)
        env["PULUMI_BACKEND_URL"] = f"file://{d}"
        where = str(d)
    elif t == "postgres":
        url = _with_params(_sops(s, backend["urlSecret"], ev, st), backend.get("urlParams") or {})
        env["PULUMI_BACKEND_URL"] = ev.secret(url)
        where = "postgres (connection string from sops)"
    elif t == "s3":
        env["PULUMI_BACKEND_URL"] = backend["url"]
        for k, v in (backend.get("env") or {}).items():
            env[k] = _sops(s, v, ev, st)
        where = backend["url"]
    else:
        raise RenderError(f"stack {stack}: unknown backend type {t}")
    ev.emit("infra", "backend", stack=stack, type=t, where=where)
    return env
