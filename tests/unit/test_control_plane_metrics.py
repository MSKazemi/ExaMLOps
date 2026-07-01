# tests/unit/test_control_plane_metrics.py
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from prometheus_client import REGISTRY

sys.path.insert(0, str(Path(__file__).parents[2] / "platform/services/control_plane"))
import metrics as _metrics  # noqa: E402


def test_record_created_increments_counter_and_sets_gauge():
    before = (
        REGISTRY.get_sample_value(
            "examlops_approval_events_total",
            {"model_id": "TESTCREATE", "action": "created"},
        )
        or 0.0
    )
    _metrics.record_created("TESTCREATE", 3)
    after = REGISTRY.get_sample_value(
        "examlops_approval_events_total",
        {"model_id": "TESTCREATE", "action": "created"},
    )
    assert after == before + 1.0
    assert REGISTRY.get_sample_value("examlops_approvals_pending") == 3.0


def test_record_approved_increments_counter_and_sets_gauge():
    before = (
        REGISTRY.get_sample_value(
            "examlops_approval_events_total",
            {"model_id": "TESTAPPROVE", "action": "approved"},
        )
        or 0.0
    )
    _metrics.record_approved("TESTAPPROVE", 2)
    after = REGISTRY.get_sample_value(
        "examlops_approval_events_total",
        {"model_id": "TESTAPPROVE", "action": "approved"},
    )
    assert after == before + 1.0
    assert REGISTRY.get_sample_value("examlops_approvals_pending") == 2.0


def test_record_rejected_increments_counter_and_sets_gauge():
    before = (
        REGISTRY.get_sample_value(
            "examlops_approval_events_total",
            {"model_id": "TESTREJECT", "action": "rejected"},
        )
        or 0.0
    )
    _metrics.record_rejected("TESTREJECT", 0)
    after = REGISTRY.get_sample_value(
        "examlops_approval_events_total",
        {"model_id": "TESTREJECT", "action": "rejected"},
    )
    assert after == before + 1.0
    assert REGISTRY.get_sample_value("examlops_approvals_pending") == 0.0


def test_update_age_none_sets_zero():
    _metrics.update_age(None)
    assert REGISTRY.get_sample_value("examlops_approval_age_oldest_seconds") == 0.0


def test_update_age_with_naive_utc_timestamp():
    # Simulate what datetime.utcnow().isoformat() produces (naive, no +00:00)
    ts_naive = (datetime.now(UTC) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    _metrics.update_age(ts_naive)
    age = REGISTRY.get_sample_value("examlops_approval_age_oldest_seconds")
    assert age is not None
    assert 110.0 < age < 130.0  # 120 s ± 10 s for test execution time


def test_update_age_future_timestamp_clamped_to_zero():
    # A timestamp 60 seconds in the future should produce 0.0 (clamped, not negative)
    ts_future = (datetime.now(UTC) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    _metrics.update_age(ts_future)
    assert REGISTRY.get_sample_value("examlops_approval_age_oldest_seconds") == 0.0
