"""Feature-view freshness on /metrics (ADR 0017 clause 4) must say when it cannot be read.

`FeatureViewStale` selects `examlops_feature_view_stale{view}`, published from the registry on
every scrape. When that read fails the gauges keep their last values (a fabricated 0 would mean
"fresh"), and on a freshly started process there are no series at all, so the alert cannot fire.
The failed read therefore needs its own counter and alert. It used to increment the approval
store's scrape-error counter, whose alert tells the operator the *approval store* is unreadable.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _sample(body: str, name: str) -> float | None:
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        metric, _, value = line.partition(" ")
        if metric == name:
            return float(value)
    return None


def test_a_failed_freshness_read_is_counted_as_its_own_failure(cp, monkeypatch):
    client = TestClient(cp.app)
    body = client.get("/metrics").text
    feature_before = _sample(body, "examlops_feature_freshness_read_errors_total")
    approval_before = _sample(body, "examlops_metrics_scrape_errors_total")
    assert feature_before is not None, "the counter must exist before the first failure"

    import examlops.feature_store.scheduler as scheduler

    def boom(**_kw):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(scheduler, "freshness_report", boom)
    body = client.get("/metrics").text
    assert _sample(body, "examlops_feature_freshness_read_errors_total") == feature_before + 1
    assert _sample(body, "examlops_metrics_scrape_errors_total") == approval_before
    assert "examlops.feature_store.scheduler" in sys.modules
