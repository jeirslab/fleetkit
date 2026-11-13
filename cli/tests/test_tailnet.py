"""`fleetkit tailnet versions`: config, version comparison, the Tailscale and
Headscale API answers (recorded in data/), the report and its exit codes.
No network: the HTTP layer is a function the tests replace."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fleetkit_cli import tailnet as tn
from fleetkit_cli.main import cli

DATA = Path(__file__).parent / "data"
FLEET = {"homelab-lan-dumpy", "xgcs-rippy", "homelab-caddy"}


def recorded(name):
    return json.loads((DATA / name).read_text())


def fake_fetch(calls=None):
    def fetch(method, url, headers, body):
        if calls is not None:
            calls.append((method, url, headers, body))
        if url.endswith("/api/v2/tailnet/-/devices?fields=all"):
            return recorded("tailscale-devices.json")
        if url.endswith("/api/v2/tailnet/-/settings"):
            return recorded("tailscale-settings.json")
        if url.endswith("/api/v1/node"):
            return recorded("headscale-nodes.json")
        raise AssertionError(url)
    return fetch


TS = tn.Tailnet("homelab", "tailscale", tn.TAILSCALE_API, {"env": "X"})
HS = tn.Tailnet("xgcs", "headscale", "https://vpn.example", {"env": "X"})


def test_parse_version():
    assert tn.parse_version("1.102.5-1-t8908149ca-gb0b35acad") == (1, 102, 5)
    assert tn.parse_version("1.66.4-1 (OpenWrt)") == (1, 66, 4)
    assert tn.parse_version("v1.104") == (1, 104, 0)
    assert tn.parse_version("") is None and tn.parse_version("unknown") is None
    assert tn.stale("1.102.2-tabc", "1.104.2") is True
    assert tn.stale("1.104.2-tabc", "1.104.2") is False
    assert tn.stale("", "1.104.2") is None


def test_latest_reads_version_json_or_source_json():
    cfg = {"repo": "r", "ref": "main", "paths": ["pkgs/tailscale/version.json", "pkgs/tailscale/source.json"]}
    files = {"pkgs/tailscale/version.json": '{"version": "1.104.2", "hashes": {}}'}
    assert tn.package_latest(cfg, files.get) == "1.104.2"
    files = {"pkgs/tailscale/source.json": '{"version": "1.100.0"}'}  # version.json absent -> None
    assert tn.package_latest(cfg, files.get) == "1.100.0"
    with pytest.raises(tn.TailnetError):
        tn.package_latest(cfg, {}.get)


def test_fleet_host_matching():
    assert tn.is_fleet_host("dumpy.tamarin-gray.ts.net", FLEET)
    assert tn.is_fleet_host("rippy", FLEET)
    assert not tn.is_fleet_host("s22", FLEET)
    assert not tn.is_fleet_host("py", FLEET)  # a suffix of a label is not a host


def test_tailscale_report():
    calls = []
    rep = tn.collect([TS], "1.104.2", "1.104.2", FLEET, lambda t: "KEY", fake_fetch(calls))
    by = {r.node: r for r in rep.rows}
    assert by["dumpy"].stale is False and by["dumpy"].fleet_host
    assert by["rippy"].stale is True and by["rippy"].fleet_host
    assert by["s22"].stale is True and not by["s22"].fleet_host
    assert by["mystery"].stale is None and by["mystery"].version == "unknown"
    assert by["dumpy"].auto_update == "off"
    assert {r.node for r in rep.stale_fleet()} == {"rippy", "caddy"}
    assert all(h["Authorization"] == "Bearer KEY" for _, _, h, _ in calls)
    assert not rep.changed and all(c[0] == "GET" for c in calls)


def test_set_auto_update_default_patches_only_when_off():
    calls = []
    rep = tn.collect([TS], "1.104.2", None, FLEET, lambda t: "KEY", fake_fetch(calls), set_auto=True)
    patch = [c for c in calls if c[0] == "PATCH"]
    assert len(patch) == 1 and patch[0][1].endswith("/api/v2/tailnet/-/settings")
    assert json.loads(patch[0][3]) == {"devicesAutoUpdatesOn": True}
    assert rep.changed


def test_headscale_nodes_without_versions():
    rep = tn.collect([HS], "1.104.2", None, {"xgcs-vpn"}, lambda t: "KEY", fake_fetch())
    by = {r.node: r for r in rep.rows}
    assert by["vpn"].fleet_host and by["vpn"].stale is None and by["vpn"].online is True
    assert "does not report" in rep.tailnets["xgcs"]["note"]
    assert not rep.stale_fleet()


def test_headscale_reads_a_version_when_the_server_sends_one():
    def fetch(m, u, h, b):
        return {"nodes": [{"givenName": "a", "hostinfo": {"IPNVersion": "1.90.0", "OS": "linux"}}]}
    rep = tn.collect([HS], "1.104.2", None, set(), lambda t: "K", fetch)
    assert rep.rows[0].stale is True and rep.rows[0].os == "linux"


def test_one_tailnet_failing_does_not_stop_the_others():
    def key(t):
        if t.kind == "headscale":
            raise tn.TailnetError("no key")
        return "K"
    rep = tn.collect([TS, HS], "1.104.2", None, FLEET, key, fake_fetch())
    assert rep.tailnets["xgcs"]["error"] == "no key" and rep.rows
    assert "ERROR no key" in tn.render_text(rep)


def test_render_table_markdown_json():
    rep = tn.collect([TS], "1.104.2", "1.104.3", FLEET, lambda t: "K", fake_fetch())
    text = tn.render_text(rep)
    assert "rippy" in text and "NixOS host" in text and "upstream release: 1.104.3" in text
    md = tn.render_markdown(rep)
    assert md.startswith("## Tailnet client versions") and "| rippy |" in md
    j = rep.to_json()
    assert j["latest"] == "1.104.2" and {n["node"] for n in j["nodes"]} >= {"dumpy", "rippy"}
    assert "K" not in json.dumps(j)


def test_secret_from_env_and_sops(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_KEY", " abc \n")
    assert tn.resolve_secret(tmp_path, {"env": "TS_KEY"}) == "abc"
    monkeypatch.delenv("TS_KEY")
    with pytest.raises(tn.TailnetError):
        tn.resolve_secret(tmp_path, {"env": "TS_KEY"})
    fake = tmp_path / "sops"
    fake.write_text('#!/bin/sh\n[ "$1 $2" = "-d --extract" ] && [ "$3" = \'["a"]["b"]\' ] && echo value\n')
    fake.chmod(0o755)
    assert tn.resolve_secret(tmp_path, {"file": "f.yaml", "key": "a/b"}, str(fake)) == "value"
    assert tn.resolve_secret(tmp_path, {"env": "TS_KEY", "file": "f.yaml", "key": "a/b"}, str(fake), tmp_path / "x") == "value"
    monkeypatch.setenv("TS_KEY", "fromenv")
    assert tn.resolve_secret(tmp_path, {"env": "TS_KEY", "file": "f.yaml", "key": "a/b"}, str(fake)) == "fromenv"
    monkeypatch.delenv("TS_KEY")
    with pytest.raises(tn.TailnetError):
        tn.resolve_secret(tmp_path, {"file": "f.yaml", "key": "a/c"}, str(fake))


def test_config(tmp_path):
    (tmp_path / "fleetkit.toml").write_text(
        '[[tailnets]]\nname = "a"\nkind = "tailscale"\nsecret = { env = "K" }\n'
        '[[tailnets]]\nname = "b"\nkind = "headscale"\nurl = "https://h/"\n'
        'secret = { file = "s.yaml", key = "x/y" }\n')
    nets, latest = tn.load_config(tmp_path)
    assert [(n.name, n.url) for n in nets] == [("a", tn.TAILSCALE_API), ("b", "https://h")]
    assert latest["repo"] == "jeirslab/nixpkgs"
    (tmp_path / "fleetkit.toml").write_text('[[tailnets]]\nname = "c"\nkind = "wireguard"\n')
    with pytest.raises(tn.TailnetError):
        tn.load_config(tmp_path)


@pytest.mark.parametrize("fail,code", [(False, 0), (True, 1)])
def test_cli_exit_codes(tmp_path, monkeypatch, fail, code):
    (tmp_path / "fleetkit.toml").write_text('[[tailnets]]\nname = "homelab"\nkind = "tailscale"\nsecret = { env = "K" }\n')
    monkeypatch.setenv("K", "key")
    monkeypatch.setattr(tn, "http_json", fake_fetch())
    monkeypatch.setattr(tn, "collect", (lambda orig: lambda *a, **kw: orig(*a, **{**kw, "fetch": fake_fetch()}))(tn.collect))
    monkeypatch.setattr(tn, "package_latest", lambda cfg, fetch=None: "1.104.2")
    monkeypatch.setattr(tn, "upstream_latest", lambda fetch=None: "1.104.2")
    summary, jf = tmp_path / "summary.md", tmp_path / "r.json"
    args = ["--flake", str(tmp_path), "tailnet", "versions", "--fleet-host", "xgcs-rippy",
            "--summary-file", str(summary), "--json-file", str(jf)] + (["--fail-on-stale-fleet"] if fail else [])
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == code, r.output
    assert "rippy" in r.output and "Tailnet client versions" in summary.read_text()
    assert json.loads(jf.read_text())["latest"] == "1.104.2"
