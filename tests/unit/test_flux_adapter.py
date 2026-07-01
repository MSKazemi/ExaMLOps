"""Unit tests for the Flux scheduler adapter (offline — no real cluster).

A ``FakeExecutor`` records the argv it is asked to run and returns canned output, so we
can assert the exact ``flux batch`` command, F58 job-id parsing, state normalization, and
that the inherited wait loop terminates on a terminal Flux state.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

from executor import CompletedCommand  # noqa: E402
from flux_adapter import FluxAdapter, _to_fsd  # noqa: E402
from scheduler import JobNotFoundError  # noqa: E402


class FakeExecutor:
    def __init__(self, canned: dict[tuple, CompletedCommand] | None = None):
        self.canned = canned or {}
        self.calls: list[list[str]] = []
        self.puts: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []

    def run(self, cmd, *, timeout=None, cwd=None):
        self.calls.append(list(cmd))
        key = tuple(cmd[:2])
        if tuple(cmd) in self.canned:
            return self.canned[tuple(cmd)]
        if key in self.canned:
            return self.canned[key]
        return CompletedCommand(0, "", "")

    def put(self, local, remote):
        self.puts.append((local, remote))

    def get(self, remote, local):
        self.gets.append((remote, local))
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_text("log")

    def close(self):
        pass


def _script(tmp) -> str:
    p = Path(tmp) / "run.sh"
    p.write_text("#!/bin/bash\necho hi\n")
    return str(p)


def _adapter(canned=None):
    return FluxAdapter(
        executor=FakeExecutor(canned),
        working_dir=tempfile.mkdtemp(),
        remote_workdir="/remote/jobs",
    )


@pytest.mark.parametrize(
    "value,expected",
    [("2:00:00", "7200s"), ("30:00", "1800s"), ("30m", "30m"), ("120", "120")],
)
def test_to_fsd(value, expected):
    assert _to_fsd(value) == expected


def test_submit_builds_flux_batch_argv_and_parses_f58():
    canned = {("flux", "batch"): CompletedCommand(0, "ƒAbCdEf\n", "")}
    a = _adapter(canned)
    with tempfile.TemporaryDirectory() as tmp:
        job_id = a.submit_job(
            script_path=_script(tmp),
            resources={
                "nodes": 2,
                "ntasks": 2,
                "cpus_per_task": 4,
                "gpus": "0",  # zero → no -g flag
                "time": "2:00:00",
                "job_name": "examlops_jpcp",
                "mem": "16G",  # dropped for flux
            },
        )
    assert job_id == "ƒAbCdEf"
    batch = [c for c in a.executor.calls if c[:2] == ["flux", "batch"]][0]
    assert "-N2" in batch and "-n2" in batch and "-c4" in batch and "-t7200s" in batch
    assert "--job-name=examlops_jpcp" in batch
    assert not any(f.startswith("-g") for f in batch)  # gpus=0 omitted
    assert not any("--mem" in f or f == "16G" for f in batch)  # mem dropped


def test_submit_emits_gpu_flag_when_positive():
    canned = {("flux", "batch"): CompletedCommand(0, "ƒX\n", "")}
    a = _adapter(canned)
    with tempfile.TemporaryDirectory() as tmp:
        a.submit_job(script_path=_script(tmp), resources={"gpus": "4"})
    batch = [c for c in a.executor.calls if c[:2] == ["flux", "batch"]][0]
    assert "-g4" in batch


def test_submit_stages_script_and_honors_remote_dir():
    canned = {("flux", "batch"): CompletedCommand(0, "ƒX\n", "")}
    a = _adapter(canned)
    with tempfile.TemporaryDirectory() as tmp:
        a.submit_job(script_path=_script(tmp), resources={}, remote_dir="/remote/jobs/abc")
    assert a.executor.puts and a.executor.puts[0][1] == "/remote/jobs/abc/run.sh"
    assert a.remote_jobdir("ƒX") == "/remote/jobs/abc"


def test_submit_failure_raises():
    from flux_adapter import JobSubmissionError  # noqa: PLC0415

    canned = {("flux", "batch"): CompletedCommand(1, "", "flux: unknown queue")}
    a = _adapter(canned)
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(JobSubmissionError, match="unknown queue"):
            a.submit_job(script_path=_script(tmp), resources={})


@pytest.mark.parametrize(
    "state,result,expected",
    [
        ("RUN", "", "RUNNING"),
        ("CLEANUP", "", "RUNNING"),
        ("SCHED", "", "PENDING"),
        ("DEPEND", "", "PENDING"),
        ("INACTIVE", "COMPLETED", "COMPLETED"),
        ("INACTIVE", "FAILED", "FAILED"),
        ("INACTIVE", "CANCELED", "CANCELLED"),
        ("INACTIVE", "TIMEOUT", "TIMEOUT"),
    ],
)
def test_state_normalization(state, result, expected):
    assert FluxAdapter._normalize(state, result) == expected


def test_get_job_status_parses_flux_jobs_line():
    line = "INACTIVE COMPLETED 2026-07-01T10:00:00 2026-07-01T10:30:00 0\n"
    canned = {("flux", "jobs"): CompletedCommand(0, line, "")}
    a = _adapter(canned)
    status = a.get_job_status("ƒX")
    assert status["state"] == "COMPLETED"
    assert status["exit_code"] == 0
    assert status["start_time"] == "2026-07-01T10:00:00"
    assert status["end_time"] == "2026-07-01T10:30:00"


def test_get_job_status_falls_back_to_eventlog_then_raises():
    # Empty `flux jobs` AND empty eventlog → JobNotFoundError.
    canned = {
        ("flux", "jobs"): CompletedCommand(0, "", ""),
        ("flux", "job"): CompletedCommand(0, "", ""),
    }
    a = _adapter(canned)
    with pytest.raises(JobNotFoundError):
        a.get_job_status("ƒGONE")


def test_wait_until_complete_terminates_on_inactive():
    line = "INACTIVE COMPLETED 2026-07-01T10:00:00 2026-07-01T10:30:00 0\n"
    canned = {("flux", "jobs"): CompletedCommand(0, line, "")}
    a = _adapter(canned)
    out = a.wait_until_complete("ƒX", poll_interval=0)
    assert out.endswith("ƒX.out")
