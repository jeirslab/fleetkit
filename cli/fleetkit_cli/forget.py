"""Forget: drop one resource from a stack's state, touching nothing real.

For a resource a stack once had and no longer declares, which the engine still
holds (protected) and so refuses to delete: a retired guest whose Proxmox guest
is long gone makes every preview fail with "cannot be deleted because it is
protected". The way out is `pulumi state delete`, which this command does
behind the checks that make it safe:

  1. the URN is not in the rendered program of the stack (a resource the
     program declares is never forgotten: the next up would create it again,
     and a live one would be created a second time);
  2. the URN is in the stack's state;
  3. a Proxmox guest (container or VM) must be gone from the cluster: its vmid
     (the state id) is looked up in the Proxmox API of the cluster the provider
     resource in state points at, with that provider's own endpoint and token.
     Anything that cannot be established (no provider, no token, an API error)
     refuses: the state entry of a live guest is what keeps a deploy from
     creating a second one;
  4. with apply only: `pulumi state unprotect`, then `pulumi state delete`
     (no --force: a resource something else in state depends on is refused by
     the engine), and a read of the state afterwards must not hold the URN.

Without apply it reports what it would remove and changes nothing.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from . import backends, guard, infra, render
from .events import Emitter
from .guests import GUEST_TYPES
from .settings import Settings

PROVIDER_PREFIX = "pulumi:providers:proxmox"


class ForgetError(Exception):
    pass


def cluster_vmids(endpoint: str, token: str, insecure: bool = False) -> set[str]:
    """The vmids of every container and VM of the cluster (GET
    /api2/json/cluster/resources?type=vm), as the provider reads them."""
    url = endpoint.rstrip("/") + "/api2/json/cluster/resources?type=vm"
    req = urllib.request.Request(url, headers={"Authorization": f"PVEAPIToken={token}"})
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            body = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgetError(f"the Proxmox API at {endpoint} could not be read ({type(e).__name__}: {e})") from None
    return {str(g["vmid"]) for g in body.get("data") or [] if g.get("vmid") is not None}


def _secret_state(st: Any) -> list[dict[str, Any]]:
    """The state with its secrets in clear (`stack export --show-secrets`)."""
    out = st._run_pulumi_cmd_sync(["stack", "export", "--show-secrets"])  # noqa: SLF001 - no public call for it
    return list((json.loads(out.stdout).get("deployment") or {}).get("resources") or [])


def _provider(resources: list[dict[str, Any]], r: dict[str, Any]) -> dict[str, Any]:
    ref = (r.get("provider") or "").rsplit("::", 1)[0]
    if ref:
        hit = [p for p in resources if p["urn"] == ref]
    else:
        hit = [p for p in resources if p["type"].startswith(PROVIDER_PREFIX)]
    if len(hit) != 1:
        raise ForgetError(f"cannot tell which Proxmox provider {r['urn']} used ({len(hit)} candidates in state), "
                          f"so the cluster cannot be asked whether its guest is gone")
    return hit[0]


def _check_gone(resources: list[dict[str, Any]], r: dict[str, Any], ev: Emitter,
                fetch: Callable[[str, str, bool], set[str]]) -> dict[str, Any]:
    vmid = str(r.get("id") or "")
    if not vmid.isdigit():
        raise ForgetError(f"{r['urn']} is a guest whose state id {vmid!r} is not a vmid: it cannot be checked "
                          f"against the cluster, so it is not forgotten")
    prov = _provider(resources, r)
    inputs = prov.get("inputs") or {}
    endpoint, token = inputs.get("endpoint"), inputs.get("apiToken")
    if not endpoint or not token or not isinstance(token, str):
        raise ForgetError(f"the provider {prov['urn']} has no endpoint and apiToken in state: the cluster cannot be "
                          f"asked whether vmid {vmid} is gone")
    ev.secret(token)
    present = fetch(endpoint, token, bool(inputs.get("insecure")))
    if vmid in present:
        raise ForgetError(f"vmid {vmid} ({r['urn']}) EXISTS in the cluster at {endpoint}: not forgotten. Delete the "
                          f"guest through the model and a deploy, or declare it again")
    return {"endpoint": endpoint, "vmid": vmid, "clusterGuests": len(present)}


def run(s: Settings, estate: str, stack: str, urn: str, ev: Emitter, apply: bool = False,
        fetch: Optional[Callable[[str, str, bool], set[str]]] = None) -> dict[str, Any]:
    """-> {estate, stack, urn, type, id, protected, apply, forgotten, checked?}; ForgetError when refused."""
    fetch = fetch or cluster_vmids
    st = render.stacks_of(s, estate, [stack])[stack]
    with render.Run(s, "forget") as rundir:
        project = render.render(s, stack, st, ev, rundir)
        program = guard.load_program(project.wd)
        declared = {u: k for k, u in guard.urns(s.stack, program).items()}
        if urn in declared:
            raise ForgetError(f"{urn} is declared by the program of {stack} (resource {declared[urn]}): only a "
                              f"resource the program no longer has is forgotten")
        env = backends.env_for(s, stack, st["backend"], ev, st)
        real = infra.open_stack(s, project.wd, stack, env, ev, install=False)
        resources = _secret_state(real)
        for e in resources:  # a provider's inputs hold the token
            tok = (e.get("inputs") or {}).get("apiToken")
            if isinstance(tok, str):
                ev.secret(tok)
        r = next((x for x in resources if x["urn"] == urn), None)
        if r is None:
            raise ForgetError(f"{urn} is not in the state of {stack}")
        report: dict[str, Any] = {"estate": estate, "stack": stack, "urn": urn, "type": r["type"],
                                  "id": r.get("id"), "protected": bool(r.get("protect")), "apply": apply,
                                  "forgotten": False}
        if r["type"].startswith(guard.STATE_ONLY):
            raise ForgetError(f"{urn}: a provider or stack resource is not forgotten by this command")
        if r["type"] in GUEST_TYPES:
            report["checked"] = _check_gone(resources, r, ev, fetch)
        if not apply:
            return report
        ev.check()
        if report["protected"]:
            infra.unprotect(real, urn)
            ev.emit("forget", "unprotect", stack=stack, urn=urn)
        real._run_pulumi_cmd_sync(["state", "delete", urn, "--yes"])  # noqa: SLF001 - no public call for it
        ev.emit("forget", "delete", stack=stack, urn=urn)
        if any(x["urn"] == urn for x in infra.state(real)):
            raise ForgetError(f"{urn} is still in the state of {stack} after `pulumi state delete`")
        report["forgotten"] = True
    return report


def text(report: dict[str, Any]) -> list[str]:
    head = f"{report['stack']}: {report['type']} {report['urn']} (id {report['id']}"
    head += ", protected)" if report["protected"] else ")"
    lines = [head]
    c = report.get("checked")
    if c:
        lines.append(f"  vmid {c['vmid']} is not among the {c['clusterGuests']} guests of {c['endpoint']}")
    lines.append("  removed from state; nothing real was touched" if report["forgotten"]
                 else "  would be removed from state (--apply to do it); nothing real is touched")
    return lines
