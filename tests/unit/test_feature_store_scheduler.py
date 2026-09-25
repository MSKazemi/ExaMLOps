"""ADR 0017 clause 4 — scheduled materialization and freshness monitoring."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_FEATURE_ONLINE_STORE", raising=False)
    from examlops.feature_store.online import reset_online_store
    from examlops.platform_db import init_db

    init_db()
    reset_online_store()


class FakeCoordinator:
    def __init__(self, held: set[str] | None = None) -> None:
        self.held = set(held or ())
        self.released: list[str] = []

    def try_lock(self, key, holder, ttl_s):
        return key not in self.held

    def unlock(self, key, holder):
        self.released.append(key)


def _views():
    from examlops import feature_store as fs

    fs.apply_view(
        fs.FeatureView("hourly", "job", ["x"], ttl_seconds=7200, materialize_interval_seconds=3600)
    )
    fs.apply_view(fs.FeatureView("manual", "job", ["x"], ttl_seconds=60))
    for v in ("hourly", "manual"):
        fs.ingest(v, "j1", "2026-09-01 10:00:00", {"x": 1.0})
    return fs


def _stamp(view: str, when: datetime) -> None:
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO feature_view_materializations (view, rows, materialized_at) VALUES (?,1,?)",
            (view, when.strftime("%Y-%m-%d %H:%M:%S")),
        )


def _audits() -> list[str]:
    from examlops.platform_db import get_db

    with get_db() as conn:
        return [
            r["target"]
            for r in conn.execute(
                "SELECT target FROM audit_events WHERE action='feature_view_materialized'"
            ).fetchall()
        ]


NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def test_only_scheduled_views_that_are_due_are_materialized():
    from examlops.feature_store.scheduler import run_materialization_cycle

    _views()
    coord = FakeCoordinator()
    out = run_materialization_cycle(now=NOW, coordinator=coord, actor="t")
    assert out["due"] == ["hourly"], "a view without an interval was scheduled"
    assert [m["view"] for m in out["materialized"]] == ["hourly"]
    assert _audits() == ["hourly"]
    assert coord.released == ["feature-materialize:hourly"]
    from examlops.platform_db import get_online_feature

    assert get_online_feature("hourly", "j1") == {"x": 1.0}
    assert get_online_feature("manual", "j1") is None


def test_a_recently_materialized_view_is_not_due_until_its_interval_elapses():
    from examlops.feature_store.scheduler import freshness_report, run_materialization_cycle

    _views()
    _stamp("hourly", NOW - timedelta(minutes=10))
    assert run_materialization_cycle(now=NOW, coordinator=FakeCoordinator())["due"] == []
    later = NOW + timedelta(hours=1)
    assert run_materialization_cycle(now=later, dry_run=True)["due"] == ["hourly"]
    report = {f.view: f for f in freshness_report(now=NOW)}
    assert report["hourly"].age_seconds == pytest.approx(600)
    assert report["hourly"].stale is False
    assert report["manual"].stale is True and report["manual"].materialized_at is None


def test_staleness_is_measured_in_utc_against_the_ttl():
    from examlops.feature_store.scheduler import freshness_report

    _views()
    _stamp("hourly", NOW - timedelta(hours=3))  # TTL is 2h
    report = {f.view: f for f in freshness_report(now=NOW)}
    assert report["hourly"].stale is True
    assert report["hourly"].age_seconds == pytest.approx(3 * 3600)


def test_dry_run_changes_nothing():
    from examlops.feature_store.scheduler import run_materialization_cycle
    from examlops.platform_db import get_online_feature

    _views()
    out = run_materialization_cycle(now=NOW, dry_run=True)
    assert out["due"] == ["hourly"] and out["materialized"] == []
    assert get_online_feature("hourly", "j1") is None
    assert _audits() == []


def test_a_view_locked_by_another_replica_is_skipped():
    from examlops.feature_store.scheduler import run_materialization_cycle

    _views()
    out = run_materialization_cycle(
        now=NOW, coordinator=FakeCoordinator({"feature-materialize:hourly"})
    )
    assert out["materialized"] == []
    assert out["skipped"] == [{"view": "hourly", "reason": "another replica holds the lock"}]


def test_one_failing_view_does_not_stop_the_cycle(monkeypatch):
    import examlops.feature_store as fs_mod
    from examlops.feature_store.scheduler import run_materialization_cycle

    fs = _views()
    fs.apply_view(fs.FeatureView("second", "job", ["x"], materialize_interval_seconds=60))
    real = fs_mod.materialize_with_index

    def flaky(view, **kw):
        if view == "hourly":
            raise RuntimeError("source unreadable")
        return real(view, **kw)

    monkeypatch.setattr(fs_mod, "materialize_with_index", flaky)
    out = run_materialization_cycle(now=NOW, coordinator=FakeCoordinator())
    assert [f["view"] for f in out["failed"]] == ["hourly"]
    assert "source unreadable" in out["failed"][0]["error"]
    assert [m["view"] for m in out["materialized"]] == ["second"]


def test_the_cycle_is_capped():
    from examlops import feature_store as fs
    from examlops.feature_store.scheduler import run_materialization_cycle

    for i in range(3):
        fs.apply_view(fs.FeatureView(f"v{i}", "job", ["x"], materialize_interval_seconds=60))
    out = run_materialization_cycle(now=NOW, coordinator=FakeCoordinator(), max_views=2)
    assert len(out["materialized"]) == 2
    assert out["skipped"] == [{"view": "v2", "reason": "cycle cap 2 reached"}]


def test_control_plane_gauges_track_views_and_drop_vanished_ones():
    sys.path.insert(0, str(ROOT / "platform" / "services" / "control_plane"))
    import metrics as cp_metrics
    from prometheus_client import REGISTRY

    from examlops.feature_store.scheduler import ViewFreshness

    rows = [
        ViewFreshness("fv_a", "2026-09-25 11:00:00", 3600.0, 60, 0, stale=True, due=False),
        ViewFreshness("fv_b", None, None, 0, 0, stale=True, due=False),  # no promise: not stale
        ViewFreshness("fv_c", None, None, 0, 300, stale=True, due=True),  # scheduled, never run
    ]
    cp_metrics.set_feature_freshness(rows)
    get = REGISTRY.get_sample_value
    assert get("examlops_feature_view_stale", {"view": "fv_a"}) == 1.0
    assert get("examlops_feature_view_age_seconds", {"view": "fv_a"}) == 3600.0
    assert get("examlops_feature_view_stale", {"view": "fv_b"}) == 0.0
    assert get("examlops_feature_view_stale", {"view": "fv_c"}) == 1.0
    assert get("examlops_feature_view_age_seconds", {"view": "fv_c"}) is None
    cp_metrics.set_feature_freshness(rows[:1])
    assert get("examlops_feature_view_stale", {"view": "fv_b"}) is None
    before = get("examlops_feature_materializations_total", {"outcome": "failed"}) or 0.0
    cp_metrics.record_feature_materialization_cycle({"failed": [{"view": "x"}], "skipped": []})
    assert get("examlops_feature_materializations_total", {"outcome": "failed"}) == before + 1


def test_a_failed_serving_tier_mirror_is_counted_not_hidden_as_success(monkeypatch):
    """The durable table materialized but Redis did not take the rows. Counting that only as
    `materialized` would leave the serving tier's failure invisible on /metrics."""
    sys.path.insert(0, str(ROOT / "platform" / "services" / "control_plane"))
    import metrics as cp_metrics
    from prometheus_client import REGISTRY

    import examlops.feature_store as fs_mod
    from examlops.feature_store.online import MirrorResult
    from examlops.feature_store.scheduler import run_materialization_cycle

    _views()
    monkeypatch.setattr(
        fs_mod,
        "materialize_with_index",
        lambda v, **_k: {"rows": 1, "online": MirrorResult("redis", error="OOM")},
    )
    out = run_materialization_cycle(now=NOW, coordinator=FakeCoordinator())
    assert [m["view"] for m in out["materialized"]] == ["hourly"]
    assert out["mirror_failed"] == [{"view": "hourly", "error": "OOM"}]

    get = REGISTRY.get_sample_value
    before = get("examlops_feature_materializations_total", {"outcome": "mirror_failed"}) or 0.0
    cp_metrics.record_feature_materialization_cycle(out)
    after = get("examlops_feature_materializations_total", {"outcome": "mirror_failed"})
    assert after == before + 1


def test_age_does_not_depend_on_the_host_timezone(monkeypatch):
    """The store stamps UTC with no zone; reading it as local time would skew every age."""
    import time

    from examlops.feature_store.scheduler import freshness_report

    _views()
    _stamp("hourly", NOW - timedelta(minutes=10))
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        report = {f.view: f for f in freshness_report(now=NOW)}
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert report["hourly"].age_seconds == pytest.approx(600)
