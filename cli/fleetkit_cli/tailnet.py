"""Tailnet client versions: `fleetkit tailnet versions`.

Asks the control servers which Tailscale client each node runs, instead of
logging in to the nodes:

  kind = "tailscale"  Tailscale's API: GET /api/v2/tailnet/-/devices?fields=all
                      (clientVersion, updateAvailable, os) and
                      GET /api/v2/tailnet/-/settings (devicesAutoUpdatesOn).
  kind = "headscale"  Headscale's API: GET /api/v1/node. Headscale (checked
                      against v0.28) does not put the client version into
                      that answer, so those nodes are listed (name, online,
                      fleet host or not) with the version "unknown", unless a
                      future server sends one (hostinfo.IPNVersion or
                      clientVersion are read when present).

"Latest" is the version the owner's package flake carries (version.json, or
source.json on older commits of it, from GitHub raw); upstream's latest
release is shown beside it for reference. A node is stale when its version is
older than that. The config is `[[tailnets]]` in the repo's fleetkit.toml:

    [tailnets_latest]                  # optional; these are the defaults
    repo = "jeirslab/nixpkgs"
    ref = "main"
    paths = ["pkgs/tailscale/version.json", "pkgs/tailscale/source.json"]

    [[tailnets]]
    name = "homelab"
    kind = "tailscale"
    url = "https://api.tailscale.com"              # default for tailscale
    secret = { env = "TAILSCALE_API_KEY",              # used when set (CI), else:
               file = "site/homelab-lan/generated.yaml",   # sops --extract at run time,
               key = "integrations/tailscale/api_key" }    # under secrets_root

    secrets_root = "../fleet-secrets"   # optional; FLEETKIT_SECRETS_ROOT overrides it

Secrets are never printed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

TAILSCALE_API = "https://api.tailscale.com"
DEFAULT_LATEST = {"repo": "jeirslab/nixpkgs", "ref": "main",
                  "paths": ["pkgs/tailscale/version.json", "pkgs/tailscale/source.json"]}


class TailnetError(Exception):
    pass


# ---------------------------------------------------------------- config

@dataclass
class Tailnet:
    name: str
    kind: str
    url: str
    secret: dict


def load_config(repo: Path) -> tuple[list[Tailnet], dict]:
    conf = repo / "fleetkit.toml"
    if not conf.is_file():
        raise TailnetError(f"{conf} not found")
    data = tomllib.loads(conf.read_text())
    out = []
    for t in data.get("tailnets", []):
        kind = t.get("kind")
        if kind not in ("tailscale", "headscale"):
            raise TailnetError(f"tailnet {t.get('name')!r}: kind is {kind!r}; tailscale or headscale")
        url = t.get("url") or (TAILSCALE_API if kind == "tailscale" else None)
        if not url or not t.get("name"):
            raise TailnetError("a [[tailnets]] table needs name and (for headscale) url")
        secret = t.get("secret")
        if not isinstance(secret, dict) or not (("env" in secret) or ("file" in secret and "key" in secret)):
            raise TailnetError(f"tailnet {t['name']!r}: secret needs env = NAME, or file = PATH and key = \"a/b/c\" "
                               "(or both: the environment first, the sops file when it is not set)")
        out.append(Tailnet(t["name"], kind, url.rstrip("/"), secret))
    if not out:
        raise TailnetError(f"{conf} has no [[tailnets]]")
    latest = {**DEFAULT_LATEST, **data.get("tailnets_latest", {})}
    if data.get("secrets_root"):
        latest["secrets_root"] = data["secrets_root"]
    return out, latest


def resolve_secret(repo: Path, secret: dict, sops: str = "sops", root: Path | None = None) -> str:
    """The environment variable when it is set, else the sops file (relative paths: under root, else the repo)."""
    if secret.get("env") and os.environ.get(secret["env"], "").strip():
        return os.environ[secret["env"]].strip()
    if "file" not in secret:
        raise TailnetError(f"environment variable {secret['env']} is not set")
    f = Path(secret["file"])
    if not f.is_absolute():
        f = (root or repo) / f
    extract = "".join(f'["{p}"]' for p in secret["key"].split("/"))
    r = subprocess.run([sops, "-d", "--extract", extract, str(f)], capture_output=True, text=True)
    if r.returncode != 0:
        # sops' own message names the file and the missing key, never a value.
        raise TailnetError(f"sops could not read {secret['key']} from {f}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


# ---------------------------------------------------------------- http

Fetch = Callable[[str, str, dict[str, str], bytes | None], dict]


def http_json(method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise TailnetError(f"{method} {url}: HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise TailnetError(f"{method} {url}: {e.reason}") from None
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- versions

_VER = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    """1.102.5-1-t8908149ca-gb0b35acad -> (1, 102, 5); None when there is none."""
    m = _VER.match((v or "").strip().lstrip("v"))
    return (int(m[1]), int(m[2]), int(m[3] or 0)) if m else None


def latest_from_json(text: str) -> str:
    d = json.loads(text)
    v = d.get("version") if isinstance(d, dict) else None
    if not v:
        raise TailnetError("no version in the package file")
    return v


def package_latest(cfg: dict, fetch: Callable[[str], str | None] | None = None) -> str:
    """The version of tailscale in the owner's package flake (first of cfg paths that exists)."""
    def raw(path: str) -> str | None:
        url = f"https://raw.githubusercontent.com/{cfg['repo']}/{cfg['ref']}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise TailnetError(f"{url}: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise TailnetError(f"{url}: {e.reason}") from None

    get = fetch or raw
    for p in cfg["paths"]:
        text = get(p)
        if text is not None:
            return latest_from_json(text)
    raise TailnetError(f"none of {cfg['paths']} exists in {cfg['repo']}@{cfg['ref']}")


def upstream_latest(fetch: Fetch = http_json) -> str | None:
    """Upstream's latest release; None when GitHub cannot be asked (it is only for reference)."""
    headers = {}
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        tag = fetch("GET", "https://api.github.com/repos/tailscale/tailscale/releases/latest", headers, None)["tag_name"]
    except (TailnetError, KeyError):
        return None
    return tag.lstrip("v")


# ---------------------------------------------------------------- rows

@dataclass
class Row:
    tailnet: str
    node: str
    os: str
    version: str
    latest: str
    stale: bool | None  # None: the version is unknown
    fleet_host: bool
    online: bool | None = None
    auto_update: str = ""  # tailnet default of the control server, "" when it has none to report


def short(name: str) -> str:
    return name.split(".")[0].lower()


def is_fleet_host(node: str, fleet_hosts: set[str]) -> bool:
    """A NixOS configuration is named <estate>-<host> or <site>-<host>; the node is the host."""
    n = short(node)
    return any(h == n or h.endswith("-" + n) for h in fleet_hosts)


def stale(version: str | None, latest: str) -> bool | None:
    v, l = parse_version(version), parse_version(latest)
    if v is None or l is None:
        return None
    return v < l


def tailscale_rows(t: Tailnet, key: str, latest: str, fleet_hosts: set[str], fetch: Fetch) -> tuple[list[Row], dict]:
    h = {"Authorization": f"Bearer {key}"}
    devs = fetch("GET", f"{t.url}/api/v2/tailnet/-/devices?fields=all", h, None).get("devices", [])
    settings = fetch("GET", f"{t.url}/api/v2/tailnet/-/settings", h, None)
    auto = "on" if settings.get("devicesAutoUpdatesOn") else "off"
    rows = []
    for d in devs:
        node = d.get("name") or d.get("hostname") or "?"
        v = d.get("clientVersion") or ""
        rows.append(Row(t.name, short(node), d.get("os", ""), v or "unknown", latest, stale(v, latest),
                        is_fleet_host(node, fleet_hosts), d.get("connectedToControl"), auto))
    return rows, {"devicesAutoUpdatesOn": settings.get("devicesAutoUpdatesOn")}


def headscale_rows(t: Tailnet, key: str, latest: str, fleet_hosts: set[str], fetch: Fetch) -> tuple[list[Row], dict]:
    nodes = fetch("GET", f"{t.url}/api/v1/node", {"Authorization": f"Bearer {key}"}, None).get("nodes", [])
    rows = []
    for n in nodes:
        info = n.get("hostinfo") or n.get("hostInfo") or {}
        v = n.get("clientVersion") or info.get("IPNVersion") or info.get("ipnVersion") or ""
        node = n.get("givenName") or n.get("given_name") or n.get("name") or "?"
        rows.append(Row(t.name, short(node), info.get("OS") or info.get("os") or "", v or "unknown", latest,
                        stale(v, latest), is_fleet_host(node, fleet_hosts), n.get("online")))
    return rows, {}


def set_auto_update_default(t: Tailnet, key: str, fetch: Fetch) -> bool:
    """PATCH /api/v2/tailnet/-/settings {"devicesAutoUpdatesOn": true}; only for Tailscale."""
    if t.kind != "tailscale":
        raise TailnetError(f"{t.name}: auto-update default is a Tailscale cloud setting")
    fetch("PATCH", f"{t.url}/api/v2/tailnet/-/settings",
          {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
          json.dumps({"devicesAutoUpdatesOn": True}).encode())
    return True


def fleet_hosts_from_flake(repo: Path, nix: str = "nix") -> set[str]:
    r = subprocess.run([nix, "eval", "--json", ".#nixosConfigurations", "--apply", "builtins.attrNames"],
                       cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise TailnetError(f"nix eval of the NixOS configurations failed: {r.stderr.strip()[-300:]}")
    return {h.lower() for h in json.loads(r.stdout)}


@dataclass
class Report:
    latest: str
    upstream: str | None
    rows: list[Row] = field(default_factory=list)
    tailnets: dict = field(default_factory=dict)  # name -> {"kind", "error"?, "settings"}
    changed: list[str] = field(default_factory=list)

    def stale_fleet(self) -> list[Row]:
        return [r for r in self.rows if r.fleet_host and r.stale]

    def to_json(self) -> dict:
        return {"latest": self.latest, "upstream_latest": self.upstream, "tailnets": self.tailnets,
                "changed": self.changed, "nodes": [asdict(r) for r in self.rows]}


def collect(tailnets: list[Tailnet], latest: str, upstream: str | None, fleet_hosts: set[str],
            secret: Callable[[Tailnet], str], fetch: Fetch = http_json, set_auto: bool = False) -> Report:
    rep = Report(latest, upstream)
    for t in tailnets:
        info: dict = {"kind": t.kind}
        rep.tailnets[t.name] = info
        try:
            key = secret(t)
            fn = tailscale_rows if t.kind == "tailscale" else headscale_rows
            rows, settings = fn(t, key, latest, fleet_hosts, fetch)
            rep.rows += rows
            info["settings"] = settings
            info["nodes"] = len(rows)
            if t.kind == "headscale" and rows and all(r.stale is None for r in rows):
                info["note"] = "Headscale's API does not report client versions"
            if set_auto and t.kind == "tailscale" and not settings.get("devicesAutoUpdatesOn"):
                set_auto_update_default(t, key, fetch)
                rep.changed.append(f"{t.name}: devicesAutoUpdatesOn set to true")
        except TailnetError as e:
            info["error"] = str(e)
    return rep


# ---------------------------------------------------------------- output

def _order(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=lambda r: (r.tailnet, not r.fleet_host, r.stale is not True, r.node))


def _cells(r: Row) -> list[str]:
    return [r.tailnet, r.node, r.os, r.version.split("-")[0] if parse_version(r.version) else r.version,
            r.latest, {True: "yes", False: "no", None: "?"}[r.stale], "NixOS host" if r.fleet_host else "other",
            {True: "up", False: "down", None: ""}[r.online], r.auto_update]


HEAD = ["tailnet", "node", "os", "version", "latest", "stale", "kind", "online", "auto-update"]


def render_text(rep: Report) -> str:
    rows = [HEAD] + [_cells(r) for r in _order(rep.rows)]
    w = [max(len(x[i]) for x in rows) for i in range(len(HEAD))]
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(x)).rstrip() for x in rows]
    return "\n".join(lines + [""] + summary_lines(rep))


def render_markdown(rep: Report) -> str:
    out = ["## Tailnet client versions", "", *summary_lines(rep, md=True), "",
           "| " + " | ".join(HEAD) + " |", "|" + "---|" * len(HEAD)]
    out += ["| " + " | ".join(_cells(r)) + " |" for r in _order(rep.rows)]
    return "\n".join(out) + "\n"


def summary_lines(rep: Report, md: bool = False) -> list[str]:
    n = len(rep.rows)
    st = [r for r in rep.rows if r.stale]
    fl = rep.stale_fleet()
    lines = [f"latest (package flake): {rep.latest}; upstream release: {rep.upstream or 'unknown'}",
             f"{n} nodes, {len(st)} stale, {len(fl)} stale NixOS hosts"
             + (": " + ", ".join(f"{r.tailnet}/{r.node}" for r in fl) if fl else "")]
    for name, info in rep.tailnets.items():
        if "error" in info:
            lines.append(f"{name}: ERROR {info['error']}")
        elif info.get("note"):
            lines.append(f"{name}: {info['note']}")
        elif info.get("settings", {}).get("devicesAutoUpdatesOn") is not None:
            lines.append(f"{name}: tailnet-wide device auto-update default is "
                         f"{'on' if info['settings']['devicesAutoUpdatesOn'] else 'off'}")
    lines += rep.changed
    return [f"- {x}" for x in lines] if md else lines
