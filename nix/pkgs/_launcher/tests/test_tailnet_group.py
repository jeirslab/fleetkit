"""Tests for `fleet tailnet` — headscale node queries and route approval.

Pure/sandbox-safe: every test stubs `headscale_api` at the module boundary, so
nothing here opens a socket or reads SOPS. What is worth pinning is the
arithmetic around the API's REPLACEMENT semantics — the approve call overwrites
a node's approved set, so a bug there silently un-approves a working route
rather than raising.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from fleet_launcher import headscale_api, tailnet_group
from fleet_launcher.main import fleet


@pytest.fixture
def node():
    return {
        "id": "7",
        "givenName": "dell-2",
        "name": "dell-2",
        "online": True,
        "availableRoutes": ["192.168.222.0/24", "10.41.0.0/24", "10.43.0.0/24"],
        "approvedRoutes": ["192.168.222.0/24"],
    }


@pytest.fixture
def stub(monkeypatch, node):
    """Patch the API surface and record what approve_routes was sent."""
    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(tailnet_group, "_base_url", lambda: "https://vpn.example.dev")
    monkeypatch.setattr(headscale_api, "list_nodes", lambda _url: [node])
    monkeypatch.setattr(headscale_api, "find_node", lambda _url, _n: node)
    monkeypatch.setattr(headscale_api, "approve_routes",
                        lambda _url, nid, routes: sent.append((nid, routes)) or {})
    return sent


# ── the replacement-semantics arithmetic ──────────────────────────────

def test_approve_sends_union_not_just_the_new_route(stub):
    # The API REPLACES approvedRoutes. Approving one route must not withdraw
    # the one already approved — that would take a working subnet router down
    # as a side effect of adding a second subnet to it.
    result = CliRunner().invoke(
        fleet, ["tailnet", "approve-routes", "dell-2", "--route", "10.43.0.0/24", "-y"])
    assert result.exit_code == 0, result.output
    assert stub == [("7", ["10.43.0.0/24", "192.168.222.0/24"])]


def test_approve_with_no_route_takes_everything_advertised(stub):
    result = CliRunner().invoke(fleet, ["tailnet", "approve-routes", "dell-2", "-y"])
    assert result.exit_code == 0, result.output
    assert stub == [("7", ["10.41.0.0/24", "10.43.0.0/24", "192.168.222.0/24"])]


def test_replace_drops_routes_not_named(stub):
    result = CliRunner().invoke(
        fleet, ["tailnet", "approve-routes", "dell-2",
                "--route", "10.43.0.0/24", "--replace", "-y"])
    assert result.exit_code == 0, result.output
    assert stub == [("7", ["10.43.0.0/24"])]


def test_replace_without_a_route_is_refused(stub):
    # Would otherwise un-approve everything the node serves.
    result = CliRunner().invoke(
        fleet, ["tailnet", "approve-routes", "dell-2", "--replace", "-y"])
    assert result.exit_code == 1
    assert "--replace needs at least one --route" in result.output
    assert stub == []


# ── refusals ──────────────────────────────────────────────────────────

def test_unadvertised_route_is_refused(stub):
    # headscale ACCEPTS approving a route the node does not advertise: it lands
    # in approvedRoutes and never in subnetRoutes, so the command would report
    # success for a route carrying no traffic.
    result = CliRunner().invoke(
        fleet, ["tailnet", "approve-routes", "dell-2", "--route", "10.99.0.0/24", "-y"])
    assert result.exit_code == 1
    assert "does not advertise" in result.output
    assert stub == []


def test_already_approved_is_a_no_op(stub):
    result = CliRunner().invoke(
        fleet, ["tailnet", "approve-routes", "dell-2",
                "--route", "192.168.222.0/24", "-y"])
    assert result.exit_code == 0, result.output
    assert "already approved" in result.output
    assert stub == []


# ── listing ───────────────────────────────────────────────────────────

def test_pending_filter_keeps_nodes_with_unapproved_routes(stub):
    result = CliRunner().invoke(fleet, ["tailnet", "nodes", "--pending", "--json"])
    assert result.exit_code == 0, result.output
    assert "dell-2" in result.output


def test_pending_filter_hides_a_fully_approved_node(monkeypatch, node):
    settled = dict(node, approvedRoutes=node["availableRoutes"])
    monkeypatch.setattr(tailnet_group, "_base_url", lambda: "https://vpn.example.dev")
    monkeypatch.setattr(headscale_api, "list_nodes", lambda _url: [settled])
    result = CliRunner().invoke(fleet, ["tailnet", "nodes", "--pending"])
    assert result.exit_code == 0, result.output
    assert "no nodes" in result.output


def test_routes_of_accepts_snake_case(node):
    # The gateway renders camelCase, but snake_case shows up in fixtures and
    # behind proxies; silently reading neither yields an empty table.
    snake = {"available_routes": ["10.0.0.0/24"], "approved_routes": ["10.0.0.0/24"]}
    assert tailnet_group._routes_of(snake) == (["10.0.0.0/24"], ["10.0.0.0/24"])
    assert tailnet_group._routes_of(node)[1] == ["192.168.222.0/24"]


# ── api client ────────────────────────────────────────────────────────

def test_missing_api_key_names_the_setting(monkeypatch):
    monkeypatch.delenv("HEADSCALE_API_KEY", raising=False)
    with pytest.raises(headscale_api.HeadscaleError) as exc:
        headscale_api._key()
    assert "apiKeySopsPath" in str(exc.value)


def test_find_node_reports_candidates_when_absent(monkeypatch):
    monkeypatch.setattr(headscale_api, "list_nodes",
                        lambda _url: [{"id": "1", "givenName": "netcore"}])
    with pytest.raises(headscale_api.HeadscaleError) as exc:
        headscale_api.find_node("https://vpn.example.dev", "nope")
    assert "netcore" in str(exc.value)
