"""Tests for the native-process job runner (replaces the tmux backing).

Pure/sandbox-safe: they spawn trivial real subprocesses (`sh -c …`) and
point the job state dir at a tmpdir, so there is no tmux, network, or SOPS.
They lock the property the whole CD story rests on — a job's recorded exit
code is the TRUTH, and `run_and_wait` returns it.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def jobs(tmp_path, monkeypatch):
    """Import sessions with its state dir redirected to a tmpdir."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("FLEET_NO_SESSION", raising=False)
    monkeypatch.delenv("FLEET_SESSION_NAME", raising=False)
    import fleet_launcher.sessions as s
    importlib.reload(s)
    return s


def test_run_job_records_success(jobs):
    rc = jobs.run_job("fleet-test-ok", ["sh", "-c", "echo hello"])
    assert rc == 0
    assert jobs._job_state("fleet-test-ok") == ("DONE-OK", 0)
    assert "hello" in jobs._log_file("fleet-test-ok").read_text()


def test_run_job_records_failure_exit_code(jobs):
    rc = jobs.run_job("fleet-test-fail", ["sh", "-c", "echo boom >&2; exit 7"])
    assert rc == 7
    assert jobs._job_state("fleet-test-fail") == ("DONE-FAIL", 7)
    assert "boom" in jobs._log_file("fleet-test-fail").read_text()  # stderr merged


def test_missing_command_is_rc_127(jobs):
    rc = jobs.run_job("fleet-test-missing", ["this-binary-does-not-exist"])
    assert rc == 127
    assert jobs._job_state("fleet-test-missing")[0] == "DONE-FAIL"


def test_run_and_wait_aggregates_worst_exit_code(jobs):
    specs = [
        {"name": "fleet-test-a", "cmd": ["true"]},
        {"name": "fleet-test-b", "cmd": ["sh", "-c", "exit 3"]},
        {"name": "fleet-test-c", "cmd": ["true"]},
    ]
    rc = jobs.run_and_wait(specs)
    assert rc == 3, "aggregate must be non-zero when any job fails"
    assert jobs._job_state("fleet-test-b") == ("DONE-FAIL", 3)
    assert jobs._job_state("fleet-test-a") == ("DONE-OK", 0)


def test_run_and_wait_all_ok(jobs):
    assert jobs.run_and_wait([{"name": "fleet-only", "cmd": ["true"]}]) == 0


def test_dead_run_record_reports_stale(jobs):
    # A RUN record whose pid is gone must not read as a stuck RUN.
    jobs._write_record("fleet-test-stale", status="RUN", pid=2**31 - 1, exit_code=None)
    assert jobs._job_state("fleet-test-stale") == ("STALE", None)
