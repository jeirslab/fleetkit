"""`fleet deploy nixos apply changed` — the plan is decided from two facts
per host (what the hive evaluates to, what the host runs) and nothing else.
Both are stubbed here; the command's classification, exit codes and the
argv it hands to `apply host` are what is under test."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from fleet_launcher import nixos


@pytest.fixture
def fleet_env(monkeypatch, tmp_path):
    cache = tmp_path / ".cache" / "fleet"
    cache.mkdir(parents=True)
    hosts = {
        "alpha": {"ip": "10.0.0.1", "vmid": 1},
        "beta": {"ip": "10.0.0.2", "vmid": 2},
        "gamma": {"ip": "", "internal_ip": "10.9.0.3", "vmid": 3},
        "delta": {"ip": "10.0.0.4", "vmid": 4},
        "epsilon": {"ip": "10.0.0.5", "vmid": 5},
    }
    (cache / "hosts.json").write_text(json.dumps(hosts))
    monkeypatch.setattr(nixos, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(nixos, "fleet_cache_dir", lambda root=None: cache)
    monkeypatch.setattr(nixos, "_refresh_inventory", lambda: None)
    monkeypatch.setattr(nixos, "_hive_node_names",
                        lambda root: ["alpha", "beta", "delta", "epsilon", "gamma"])

    expected = {
        "alpha": ("/nix/store/aaa-nixos-system-alpha-lxc-26.11", ""),
        "beta": ("/nix/store/bbb2-nixos-system-beta-lxc-26.11", ""),
        "gamma": (None, "error: infinite recursion encountered"),
        "delta": ("/nix/store/ddd-nixos-system-delta-lxc-26.11", ""),
        "epsilon": ("/nix/store/eee-nixos-system-epsilon-lxc-26.11", ""),
    }
    running = {
        "10.0.0.1": ("/nix/store/aaa-nixos-system-alpha-lxc-26.11", ""),
        "10.0.0.2": ("/nix/store/bbb1-nixos-system-beta-lxc-26.11", ""),
        "10.9.0.3": ("/nix/store/ggg-nixos-system-gamma-lxc-26.11", ""),
        "10.0.0.4": (None, "ssh: connect to host 10.0.0.4 port 22: No route to host"),
        # epsilon's address answers, but as somebody else's machine.
        "10.0.0.5": ("/nix/store/zzz-nixos-system-other-estate-box-lxc-26.11", ""),
    }
    monkeypatch.setattr(nixos, "_expected_system", lambda root, n: expected[n])
    monkeypatch.setattr(nixos, "_expected_systems_bulk",
                        lambda root, ns: {n: expected[n] for n in ns})
    monkeypatch.setattr(nixos, "_running_system", lambda ip, timeout=10: running[ip])

    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def fake_run_shell(cmd, **kw):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(nixos, "run_shell", fake_run_shell)
    monkeypatch.setattr("fleet_launcher._util.fleet_executable", lambda: "fleet")
    return calls


def _run(*args):
    return CliRunner().invoke(nixos.apply_changed, list(args), catch_exceptions=False)


def test_eval_failure_and_unreachable_fail_the_run(fleet_env):
    r = _run("--dry-run", "--skip", "epsilon")
    assert r.exit_code == 1
    assert "gamma" in r.output and "do not evaluate" in r.output
    assert "delta" in r.output and "unreachable" in r.output
    assert fleet_env == []


def test_an_address_answering_as_another_host_is_refused_whatever_the_flags(fleet_env):
    r = _run("--skip", "gamma", "--unreachable", "deploy")
    assert r.exit_code == 1
    assert "wrong host" in r.output and "other-estate-box" in r.output
    assert "IP collision" in r.output
    assert fleet_env == []  # nothing deployed anywhere, not even the honest hosts


def test_skip_and_unreachable_skip_deploy_only_the_changed_host(fleet_env):
    r = _run("--skip", "gamma", "--skip", "epsilon", "--unreachable", "skip")
    assert r.exit_code == 0, r.output
    assert "leaving out unreachable: delta" in r.output
    assert len(fleet_env) == 1
    cmd = fleet_env[0]
    assert cmd[:5] == ["fleet", "deploy", "nixos", "apply", "host"]
    assert "beta" in cmd and "alpha" not in cmd and "delta" not in cmd
    assert "--wait" in cmd and "--no-refresh" in cmd
    assert cmd[cmd.index("--parallel") + 1] == "4"


def test_unreachable_deploy_includes_the_silent_host(fleet_env):
    r = _run("--skip", "gamma", "--skip", "epsilon", "--unreachable", "deploy", "--reboot")
    assert r.exit_code == 0, r.output
    cmd = fleet_env[0]
    assert "beta" in cmd and "delta" in cmd and "--reboot" in cmd


def test_dry_run_reports_without_deploying(fleet_env):
    r = _run("--skip", "gamma", "--skip", "epsilon", "--unreachable", "skip", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "deploying 1 host(s): beta" in r.output
    assert fleet_env == []


def test_only_restricts_the_set_and_nothing_to_do_is_success(fleet_env):
    r = _run("--only", "alpha")
    assert r.exit_code == 0, r.output
    assert "nothing to deploy" in r.output
    assert fleet_env == []


def test_only_unknown_host_is_an_error(fleet_env):
    r = _run("--only", "omega")
    assert r.exit_code == 1
    assert "not a node of the hive: omega" in r.output


def test_jobs_zero_evaluates_the_hive_in_one_process(fleet_env):
    r = _run("--skip", "gamma", "--skip", "epsilon", "--unreachable", "skip", "--jobs", "0", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "in one process" in r.output
    assert "deploying 1 host(s): beta" in r.output


def test_build_on_target_goes_through_apply_remote(fleet_env):
    r = _run("--skip", "gamma", "--skip", "epsilon", "--unreachable", "skip", "--build-on-target", "--reboot")
    assert r.exit_code == 0, r.output
    cmd = fleet_env[0]
    assert cmd[:5] == ["fleet", "deploy", "nixos", "apply", "remote"]
    assert "beta" in cmd and "--reboot" in cmd and "--wait" not in cmd
