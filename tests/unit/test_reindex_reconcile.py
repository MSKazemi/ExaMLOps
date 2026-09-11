# tests/unit/test_reindex_reconcile.py
"""A reindex whose scheduler job died must not read `submitted` forever (BL-053, ADR 0043 cl. 4).

On Slurm / Flux a reindex is fire-and-forget and the job settles its own row. A job that dies before
its interpreter runs never does. `exa embedding status` reconciles such rows with the scheduler —
and may only *settle* a row on a clear terminal answer, never guess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import embeddings  # noqa: E402
from examlops.embeddings import reconcile_submitted, reindex_status  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    create_reindex_job,
    list_reindex_jobs,
    update_reindex_job,
)


class _Scheduler:
    def __init__(self, states: dict[str, str], on_query=None):
        self.states = states
        self.on_query = on_query
        self.asked: list[str] = []

    def get_job_status(self, job_id):
        self.asked.append(job_id)
        if self.on_query:
            self.on_query(job_id)
        if job_id not in self.states:
            raise LookupError(f"job {job_id} not in squeue or sacct")
        return {
            "state": self.states[job_id],
            "exit_code": None,
            "start_time": None,
            "end_time": None,
        }


def _submitted(collection: str, hpc_job_id: str, tenant: str = "default") -> int:
    job_id = create_reindex_job(collection, tenant, "enc-old", "enc-new")
    update_reindex_job(job_id, status="submitted", hpc_job_id=hpc_job_id, orchestrator="scheduler")
    return job_id


def _status(collection: str) -> str:
    return list_reindex_jobs(collection)[0]["status"]


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "TIMEOUT"])
def test_a_job_that_ended_badly_fails_its_row(monkeypatch, state):
    _submitted("rc1", "41")
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Scheduler({"41": state}))

    reindex_status("rc1")

    assert _status("rc1") == "failed"


def test_completed_without_reaching_the_reindex_is_a_failure_too(monkeypatch):
    """The job exited 0 but the row is still `submitted`: the reindex never ran."""
    job_id = _submitted("rc2", "42")
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Scheduler({"42": "COMPLETED"}))

    assert reconcile_submitted("rc2") == [job_id]
    assert _status("rc2") == "failed"


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "UNKNOWN"])
def test_a_job_still_in_flight_is_left_alone(monkeypatch, state):
    _submitted("rc3", "43")
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Scheduler({"43": state}))

    reindex_status("rc3")

    assert _status("rc3") == "submitted"


def test_no_clear_answer_decides_nothing(monkeypatch):
    """No scheduler here, or a job it cannot see: reconciliation may only settle, never guess."""
    _submitted("rc4", "44")
    monkeypatch.setattr(
        embeddings, "_scheduler_adapter", lambda: (_ for _ in ()).throw(RuntimeError("no sbatch"))
    )
    reindex_status("rc4")
    assert _status("rc4") == "submitted"

    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Scheduler({}))
    reindex_status("rc4")
    assert _status("rc4") == "submitted"


def test_a_job_that_settles_its_row_while_being_asked_wins(monkeypatch):
    """The race the ordering exists for: the job writes its outcome, then exits, and the scheduler
    reports it COMPLETED. The row is re-read *after* that answer, so the outcome stands."""
    _submitted("rc5", "45")
    row_id = list_reindex_jobs("rc5")[0]["id"]
    scheduler = _Scheduler(
        {"45": "COMPLETED"}, on_query=lambda _j: update_reindex_job(row_id, status="switched")
    )
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: scheduler)

    assert reconcile_submitted("rc5") == []
    assert _status("rc5") == "switched"


def test_only_submitted_rows_are_asked_about(monkeypatch):
    _submitted("rc6", "46")
    done = create_reindex_job("rc6", "default", "a", "b")
    update_reindex_job(done, status="switched", hpc_job_id="47")
    scheduler = _Scheduler({"46": "RUNNING", "47": "FAILED"})
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: scheduler)

    reindex_status("rc6")

    assert scheduler.asked == ["46"]
    assert {r["status"] for r in list_reindex_jobs("rc6")} == {"submitted", "switched"}


def test_status_can_be_read_without_touching_the_scheduler(monkeypatch):
    _submitted("rc7", "48")
    scheduler = _Scheduler({"48": "FAILED"})
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: scheduler)

    reindex_status("rc7", reconcile=False)

    assert scheduler.asked == [] and _status("rc7") == "submitted"


def test_a_reconciled_failure_is_audited(monkeypatch):
    from examlops.data.audit import export_audit_events

    _submitted("rc8", "49")
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Scheduler({"49": "CANCELLED"}))

    reindex_status("rc8")

    events = [e for e in export_audit_events() if e["action"] == "reindex_reconciled_failed"]
    assert events and "CANCELLED" in str(events[-1]["details"])


def test_one_tenants_status_never_settles_anothers_rows(monkeypatch):
    _submitted("rc9", "50", tenant="other-tenant")
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Scheduler({"50": "FAILED"}))

    reindex_status("rc9", "default")

    assert _status("rc9") == "submitted"
