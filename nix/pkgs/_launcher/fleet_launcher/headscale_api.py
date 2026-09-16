"""Minimal headscale JSON API client — node listing and route approval.

Headscale exposes its gRPC-gateway JSON API on the SAME listener the tailscale
clients talk to, so `fleet.settings.tailnet.controlUrl` is already the base URL
and nothing new has to be published. Auth is a bearer API key
(`headscale apikeys create`, prefix `hskey-api-`) held in SOPS at
`fleet.settings.tailnet.apiKeySopsPath` and exported as HEADSCALE_API_KEY by
the launcher bootstrap.

Why an API client and not `ssh <control-host> headscale …`: approving a route
is the last manual step in bringing up a subnet router, and routing it through
SSH means an operator shell on the control plane for what is a single
authenticated POST. The key is scoped, revocable, and expiring; a root shell is
none of those.

Route model (headscale >= 0.26, verified against 0.28): routes are a property
of the NODE, not a separate collection. The pre-0.26 `/api/v1/routes/{id}/enable`
endpoints are GONE — a client written against them gets a 404 that reads like a
missing node. Each node carries:

    availableRoutes  what the node advertises (`tailscale set --advertise-routes`)
    approvedRoutes   what an operator has accepted
    subnetRoutes     the intersection — what actually carries traffic

Approval is a full REPLACEMENT of `approvedRoutes`, so callers must send the
union of what they want kept, never just the delta.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class HeadscaleError(RuntimeError):
    """A failure to REACH or authenticate headscale — an 'I could not look'
    answer, distinct from 'that node is not enrolled'."""


def _key() -> str:
    key = os.environ.get("HEADSCALE_API_KEY", "")
    if not key:
        raise HeadscaleError(
            "HEADSCALE_API_KEY unset. Set fleet.settings.tailnet.apiKeySopsPath "
            "to a SOPS key holding a headscale API key (mint one on the control "
            "host with `headscale apikeys create --expiration 90d`).")
    return key


def _request(base_url: str, path: str, method: str = "GET",
             body: dict | None = None) -> dict:
    url = f"{base_url.rstrip('/')}/api/v1{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = ": " + e.read().decode("utf-8", "replace").strip()[:300]
        except Exception:
            pass
        hint = ""
        if e.code == 401:
            # By far the most common failure, and the message headscale returns
            # ("Unauthorized") does not distinguish the two causes.
            hint = ("\n\nhint: the key is rejected — either expired "
                    "(`headscale apikeys list` on the control host) or it is a "
                    "PREAUTH key (hskey-auth-, for enrolling hosts) where an API "
                    "key (hskey-api-) is required. They are not interchangeable.")
        raise HeadscaleError(f"{method} {path}: HTTP {e.code} {e.reason}{detail}{hint}") from e
    except Exception as e:  # URLError, timeout, JSON — all "could not look"
        raise HeadscaleError(f"{method} {path}: {type(e).__name__}: {e}") from e
    return json.loads(raw) if raw.strip() else {}


def list_nodes(base_url: str) -> list[dict]:
    """Every enrolled node, verbatim from the API."""
    return _request(base_url, "/node").get("nodes") or []


def find_node(base_url: str, name: str) -> dict:
    """The one node whose `name` or `givenName` matches, else raise.

    Ambiguity raises with the candidates rather than picking one — the same
    contract `fleet dev connect` follows for service/host name collisions.
    """
    nodes = list_nodes(base_url)
    matches = [n for n in nodes
               if name in (n.get("name"), n.get("givenName"))]
    if not matches:
        known = ", ".join(sorted(
            str(n.get("givenName") or n.get("name")) for n in nodes)) or "(none enrolled)"
        raise HeadscaleError(f"no tailnet node named {name!r}. Enrolled: {known}")
    if len(matches) > 1:
        ids = ", ".join(str(n.get("id")) for n in matches)
        raise HeadscaleError(f"{name!r} matches several nodes (ids: {ids})")
    return matches[0]


def approve_routes(base_url: str, node_id: str, routes: list[str]) -> dict:
    """Set a node's approved routes to exactly `routes` (a replacement, not a
    merge — see the module docstring)."""
    return _request(base_url, f"/node/{node_id}/approve_routes",
                    method="POST", body={"routes": routes})
