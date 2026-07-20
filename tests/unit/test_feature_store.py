"""A3 — feature store & train/serve consistency (ADR 0017)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _apply():
    from examlops.feature_store import FeatureView, apply_view

    apply_view(
        FeatureView(
            name="job_features",
            entity="job",
            features=["pclass", "mbwidth"],
            ttl_seconds=3600,
            dataset_revision="rev-1",
        )
    )


def test_apply_and_get_view():
    from examlops.feature_store import get_view

    _apply()
    v = get_view("job_features")
    assert v is not None
    assert v.entity == "job"
    assert v.features == ["pclass", "mbwidth"]
    assert v.dataset_revision == "rev-1"


def test_list_views():
    from examlops.feature_store import list_views

    _apply()
    names = [v.name for v in list_views()]
    assert names == ["job_features"]


def test_r1_single_definition_projects_to_declared_features():
    from examlops.feature_store import get_training_features, ingest

    _apply()
    # Ingest with an extra field not in the view — it must be projected away.
    ingest(
        "job_features",
        "job-1",
        "2026-07-16 10:00:00",
        {"pclass": "compute-bound", "mbwidth": 42.0, "leaked": 1},
    )
    vals = get_training_features(
        "job_features", [{"entity_id": "job-1", "event_ts": "2026-07-16 12:00:00"}]
    )[0]
    assert vals == {"pclass": "compute-bound", "mbwidth": 42.0}


def test_r5_point_in_time_no_future_leak():
    from examlops.feature_store import get_training_features, ingest

    _apply()
    ingest("job_features", "job-1", "2026-07-16 10:00:00", {"pclass": "a", "mbwidth": 1.0})
    ingest("job_features", "job-1", "2026-07-16 14:00:00", {"pclass": "b", "mbwidth": 2.0})
    # As-of noon: only the 10:00 value is visible; the 14:00 value is the future.
    vals = get_training_features(
        "job_features", [{"entity_id": "job-1", "event_ts": "2026-07-16 12:00:00"}]
    )[0]
    assert vals == {"pclass": "a", "mbwidth": 1.0}


def test_r4_asof_returns_latest_before_event():
    from examlops.feature_store import get_training_features, ingest

    _apply()
    ingest("job_features", "job-1", "2026-07-16 08:00:00", {"pclass": "a", "mbwidth": 1.0})
    ingest("job_features", "job-1", "2026-07-16 09:00:00", {"pclass": "b", "mbwidth": 2.0})
    vals = get_training_features(
        "job_features", [{"entity_id": "job-1", "event_ts": "2026-07-16 09:30:00"}]
    )[0]
    assert vals == {"pclass": "b", "mbwidth": 2.0}


def test_r2_zero_skew_after_materialize():
    from examlops.feature_store import ingest, materialize, measure_skew

    _apply()
    ingest("job_features", "job-1", "2026-07-16 10:00:00", {"pclass": "x", "mbwidth": 9.0})
    materialize("job_features")
    skew = measure_skew("job_features", "job-1", "2026-07-16 12:00:00")
    assert skew["matches"] is True
    assert skew["online"] == skew["offline"] == {"pclass": "x", "mbwidth": 9.0}


def test_online_read_returns_latest_materialized():
    from examlops.feature_store import get_online_features, ingest, materialize

    _apply()
    ingest("job_features", "job-1", "2026-07-16 10:00:00", {"pclass": "old", "mbwidth": 1.0})
    materialize("job_features")
    ingest("job_features", "job-1", "2026-07-16 11:00:00", {"pclass": "new", "mbwidth": 2.0})
    materialize("job_features")
    assert get_online_features("job_features", ["job-1"])[0] == {"pclass": "new", "mbwidth": 2.0}


def test_r6_freshness_stale_flag():
    from examlops.feature_store import freshness, ingest, materialize

    _apply()  # ttl 3600
    ingest("job_features", "job-1", "2026-07-16 10:00:00", {"pclass": "x", "mbwidth": 1.0})
    materialize("job_features")
    f = freshness("job_features")
    assert f.materialized_at is not None
    # A now far in the future exceeds the TTL → stale.
    stale = freshness("job_features", now_ts="2030-01-01 00:00:00")
    assert stale.stale is True


def test_freshness_never_materialized():
    from examlops.feature_store import freshness

    _apply()
    f = freshness("job_features")
    assert f.materialized_at is None
    assert f.stale is True


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r = runner.invoke(
        app,
        [
            "feature",
            "apply",
            "jf",
            "--entity",
            "job",
            "--features",
            "pclass,mbwidth",
            "--ttl",
            "60",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "feature",
            "ingest",
            "jf",
            "--entity-id",
            "job-1",
            "--event-ts",
            "2026-07-16 10:00:00",
            "--values",
            '{"pclass": "compute-bound", "mbwidth": 3.0}',
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["feature", "materialize", "jf"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["feature", "get", "jf", "--entity-id", "job-1"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["feature", "list"])
    assert r.exit_code == 0, r.output
    assert "jf" in r.output
