"""The control plane scores drift on a timer and announces changes (plan P2.4b).

``_evaluate_drift_once`` is what the ``drift-evaluator`` thread runs every
``CONTROL_PLANE_DRIFT_EVAL_SECONDS``. A failed evaluation must be counted, not end the loop: a
silently dead evaluator would stop every drift event with nothing to show for it.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

TOKEN = "-".join(("test", "token", "0123456789"))


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "drift.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _metric(cp, line_start: str) -> float:
    body = TestClient(cp.app).get("/metrics").text
    values = [
        float(line.rsplit(" ", 1)[1]) for line in body.splitlines() if line.startswith(line_start)
    ]
    return sum(values)


def test_a_failed_evaluation_is_counted_and_the_loop_survives(cp, monkeypatch):
    from examlops import drift_status

    before = _metric(cp, "examlops_drift_evaluation_errors_total")

    def broken(**_kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(drift_status, "evaluate", broken)
    assert cp._evaluate_drift_once() == []  # returned, not raised
    assert _metric(cp, "examlops_drift_evaluation_errors_total") == before + 1


def test_changes_are_counted_by_their_new_status(cp, monkeypatch):
    from examlops import drift_status

    change = {"model": "jpcp", "previous": "OK", "status": "CRITICAL", "z_score": 4.0}
    monkeypatch.setattr(drift_status, "evaluate", lambda **_kw: [change])
    before = _metric(cp, 'examlops_drift_status_changes_total{status="CRITICAL"}')
    assert cp._evaluate_drift_once() == [change]
    assert _metric(cp, 'examlops_drift_status_changes_total{status="CRITICAL"}') == before + 1


def test_the_error_counter_exists_before_the_first_failure(cp):
    """Unlabeled, so it exports from the start and DriftEvaluationFailing sees failure one."""
    body = TestClient(cp.app).get("/metrics").text
    samples = [line for line in body.splitlines()
               if line.startswith("examlops_drift_evaluation_errors_total ")]  # fmt: skip
    assert len(samples) == 1
