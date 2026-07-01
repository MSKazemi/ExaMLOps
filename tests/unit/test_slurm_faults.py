"""Fault-injection tests for the Phase 2 Slurm adapter hardening.

Proves: the wait loop is bounded (JobTimeoutError past the deadline), a transient
UNKNOWN/absent job is tolerated then declared lost (JobNotFoundError), and a hung
scheduler CLI is converted into JobTimeoutError instead of freezing the worker.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

import adapter as slurm_adapter  # noqa: E402
import executor as executor_mod  # noqa: E402
import scheduler as scheduler_mod  # noqa: E402


def _adapter(tmp_path):
    return slurm_adapter.RealSlurmAdapter(working_dir=str(tmp_path / "jobs"))


def test_wait_raises_timeout_past_deadline(tmp_path, monkeypatch):
    a = _adapter(tmp_path)
    monkeypatch.setattr(a, "get_job_status", lambda _j: {"state": "RUNNING"})
    with pytest.raises(slurm_adapter.JobTimeoutError):
        a.wait_until_complete("123", poll_interval=0, max_wait_s=0)


def test_wait_terminates_on_completed(tmp_path, monkeypatch):
    a = _adapter(tmp_path)
    monkeypatch.setattr(a, "get_job_status", lambda _j: {"state": "COMPLETED"})
    # Must return normally (not raise) and not hang.
    assert a.wait_until_complete("123", poll_interval=0).endswith("123.out")


def test_wait_tolerates_then_gives_up_on_unknown(tmp_path, monkeypatch):
    a = _adapter(tmp_path)
    calls = {"n": 0}

    def _always_missing(_job):
        calls["n"] += 1
        raise slurm_adapter.JobNotFoundError("gone")

    monkeypatch.setattr(a, "get_job_status", _always_missing)
    monkeypatch.setattr(scheduler_mod, "_MAX_UNKNOWN_POLLS", 3)
    with pytest.raises(slurm_adapter.JobNotFoundError):
        a.wait_until_complete("123", poll_interval=0)
    assert calls["n"] == 3  # tolerated up to the streak limit, then gave up


def test_wait_recovers_after_transient_unknown(tmp_path, monkeypatch):
    a = _adapter(tmp_path)
    states = iter(
        [
            {"state": "RUNNING"},
            slurm_adapter.JobNotFoundError("blip"),  # transient
            {"state": "RUNNING"},
            {"state": "COMPLETED"},
        ]
    )

    def _next(_job):
        v = next(states)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(a, "get_job_status", _next)
    monkeypatch.setattr(scheduler_mod, "_MAX_UNKNOWN_POLLS", 3)
    # A single UNKNOWN in the middle must not abort the wait.
    assert a.wait_until_complete("123", poll_interval=0).endswith("123.out")


def test_scheduler_cmd_timeout_becomes_job_timeout(monkeypatch):
    """A hung scheduler CLI is converted to JobTimeoutError by the LocalExecutor."""

    def _hang(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="squeue", timeout=30)

    monkeypatch.setattr(executor_mod.subprocess, "run", _hang)
    with pytest.raises(slurm_adapter.JobTimeoutError):
        executor_mod.LocalExecutor().run(["squeue", "-j", "1"])
