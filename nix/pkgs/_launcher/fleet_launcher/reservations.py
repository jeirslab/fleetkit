"""Address and vmid reservations across every state in the tofu backend.

Several fleets can share one Proxmox cluster and one pg state backend while
each is declared in its own repository (XGCS on jeirslab hardware). Each
repository hands out 10.1.1.x and vmids on its own, so nothing stops two of
them declaring the same address — and the first sign is a deploy landing on
the wrong machine. This module reads what EVERY state in the backend holds
and refuses, before tofu runs, a declaration that collides with a resource
that is not the same one.

Pure functions here (parse, compare) so the rule is unit-tested; the two
I/O edges (psql, hosts.json) are small and live at the bottom.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PVE_TYPES = ("proxmox_virtual_environment_container", "proxmox_virtual_environment_vm")


@dataclass(frozen=True)
class Reservation:
    schema: str        # backend schema the state lives in (one per stack)
    address: str       # resource address, e.g. proxmox_virtual_environment_container.mcp-proxy
    name: str          # resource name (the fleet's host name)
    ips: tuple[str, ...]
    vmid: int | None
    node: str | None


@dataclass(frozen=True)
class Conflict:
    host: str          # the declared host
    what: str          # "ip 10.1.1.53" | "vmid 115"
    held_by: Reservation
    kind: str          # "reserved" (another resource) | "declared twice" (same name, other state)


def _strip_cidr(addr: str | None) -> str | None:
    if not addr or addr in ("dhcp", "manual"):
        return None
    return addr.split("/", 1)[0]


def parse_state(schema: str, state: dict) -> list[Reservation]:
    """Reservations held by one tfstate document (``tofu state pull`` shape)."""
    out: list[Reservation] = []
    for res in state.get("resources", []) or []:
        if res.get("type") not in PVE_TYPES or res.get("mode", "managed") != "managed":
            continue
        for inst in res.get("instances", []) or []:
            a = inst.get("attributes") or {}
            ips: list[str] = []
            for init in a.get("initialization") or []:
                for ipc in init.get("ip_config") or []:
                    for v4 in ipc.get("ipv4") or []:
                        ip = _strip_cidr(v4.get("address"))
                        if ip:
                            ips.append(ip)
            vmid = a.get("vm_id")
            addr = f"{res['type']}.{res['name']}"
            if inst.get("index_key") is not None:
                addr += f"[{json.dumps(inst['index_key'])}]"
            out.append(Reservation(
                schema=schema, address=addr, name=res["name"], ips=tuple(ips),
                vmid=int(vmid) if isinstance(vmid, (int, str)) and str(vmid).isdigit() else None,
                node=a.get("node_name"),
            ))
    return out


def declared_hosts(hosts: dict) -> dict[str, tuple[str | None, int | None]]:
    """``name -> (ip, vmid)`` from hosts.json (a single-internal guest keeps its
    address in ``internal_ip``)."""
    out = {}
    for name, h in hosts.items():
        ip = h.get("ip") or h.get("internal_ip") or None
        vmid = h.get("vmid")
        out[name] = (ip, int(vmid) if vmid not in (None, "") else None)
    return out


def find_conflicts(declared: dict[str, tuple[str | None, int | None]],
                   reservations: list[Reservation]) -> list[Conflict]:
    """A declared host collides with a reservation when they share an address
    or a vmid and are not the same resource. Two states holding the same
    resource name are two resources — a double declaration, reported too."""
    conflicts: list[Conflict] = []
    by_name: dict[str, list[Reservation]] = {}
    for r in reservations:
        by_name.setdefault(r.name, []).append(r)
    for host, (ip, vmid) in sorted(declared.items()):
        own_schemas = {r.schema for r in by_name.get(host, [])}
        for r in reservations:
            same_resource = r.name == host and len(own_schemas) <= 1
            if same_resource:
                continue
            kind = "declared twice" if r.name == host else "reserved"
            if ip and ip in r.ips:
                conflicts.append(Conflict(host, f"ip {ip}", r, kind))
            elif vmid is not None and r.vmid == vmid:
                conflicts.append(Conflict(host, f"vmid {vmid}", r, kind))
    # A name held in two states with the same address shows up once per host
    # above only if the host is declared here; keep one entry per (host, what).
    seen: set[tuple[str, str, str, str]] = set()
    uniq: list[Conflict] = []
    for c in conflicts:
        k = (c.host, c.what, c.held_by.schema, c.held_by.address)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


# ── I/O edges ────────────────────────────────────────────────────────

def psql_prefix(root: Path) -> list[str]:
    """``psql`` from PATH, else from nixpkgs via ``nix shell`` (the same trick
    backend-check uses: ``nix run nixpkgs#postgresql`` runs the SERVER)."""
    psql = shutil.which("psql")
    if psql:
        return [psql]
    return ["nix", "shell", "--inputs-from", str(root), "nixpkgs#postgresql", "--command", "psql"]


def fetch_backend_states(root: Path, conn: str) -> dict[str, dict]:
    """Every ``<schema>.states`` document in the pg backend, keyed by schema.
    The pg backend keeps one schema per stack, each with a ``states`` table
    whose ``data`` column is the tfstate JSON. Read-only."""
    prefix = psql_prefix(root)
    q = ("select table_schema from information_schema.tables "
         "where table_name = 'states' order by 1")
    r = subprocess.run(prefix + [conn, "-At", "-c", q],
                       capture_output=True, text=True, timeout=120, check=True)
    schemas = [s for s in r.stdout.split() if s]
    out: dict[str, dict] = {}
    for schema in schemas:
        r = subprocess.run(
            prefix + [conn, "-At", "-c", f'select data from "{schema}".states where data is not null'],
            capture_output=True, text=True, timeout=120, check=True)
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out[schema] = json.loads(line)
            except json.JSONDecodeError:
                continue
    return out


def backend_reservations(root: Path) -> list[Reservation] | None:
    """All reservations across this fleet's backend AND its peers (other
    fleets' backends on the same cluster, PG_PEER_CONN_STRS, one per line —
    from fleet.settings.backend.pg.peerConnStrSopsPaths), or None when there
    is no pg connection to ask (PG_CONN_STR is resolved by the launcher
    bootstrap). A peer's schemas are labelled ``peer<N>:<schema>``."""
    conn = os.environ.get("PG_CONN_STR")
    if not conn:
        return None
    res: list[Reservation] = []
    for schema, state in fetch_backend_states(root, conn).items():
        res.extend(parse_state(schema, state))
    peers = [p for p in os.environ.get("PG_PEER_CONN_STRS", "").splitlines() if p.strip()]
    for i, peer in enumerate(peers, 1):
        for schema, state in fetch_backend_states(root, peer).items():
            res.extend(parse_state(f"peer{i}:{schema}", state))
    return res
