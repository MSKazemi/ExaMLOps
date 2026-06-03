from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "test_platform.db")
    os.environ["PLATFORM_DB"] = p
    yield p
    os.environ.pop("PLATFORM_DB", None)
    # force module to re-evaluate path next call
    import importlib
    import examlops.platform_db as m
    importlib.reload(m)


def _get_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_init_db_creates_all_tables(db_path):
    from examlops.platform_db import init_db, get_db
    init_db()
    with get_db() as conn:
        tables = _get_tables(conn)
    assert {"audit_events", "drift_snapshots", "drift_baselines",
            "traffic_rules", "promotion_rules"} <= tables


def test_write_audit_event(db_path):
    from examlops.platform_db import init_db, get_db, write_audit_event
    init_db()
    write_audit_event("cli", "alice", "retrain_triggered", "JPCP", {"backend": "minio"})
    with get_db() as conn:
        row = conn.execute(
            "SELECT source, actor, action, target FROM audit_events"
        ).fetchone()
    assert row["source"] == "cli"
    assert row["action"] == "retrain_triggered"


def test_write_drift_snapshot(db_path):
    from examlops.platform_db import init_db, write_drift_snapshot, get_db
    init_db()
    write_drift_snapshot("JPCP", "Production", 89.45, "job-abc")
    with get_db() as conn:
        row = conn.execute(
            "SELECT model, alias, prediction FROM drift_snapshots"
        ).fetchone()
    assert row["model"] == "JPCP"
    assert abs(row["prediction"] - 89.45) < 0.001


def test_get_set_traffic_rules(db_path):
    from examlops.platform_db import init_db, set_traffic_rules, get_traffic_rules
    init_db()
    set_traffic_rules("JPCP", {"Production": 90, "Canary": 10}, "alice")
    rules = get_traffic_rules("JPCP")
    assert rules == {"Production": 90, "Canary": 10}


def test_get_traffic_rules_returns_none_if_missing(db_path):
    from examlops.platform_db import init_db, get_traffic_rules
    init_db()
    assert get_traffic_rules("JPCP") is None


def test_set_get_promotion_rule(db_path):
    from examlops.platform_db import init_db, set_promotion_rule, get_promotion_rule
    init_db()
    set_promotion_rule("JPCP", "rmse", "lt", 5.0, "Staging", "Production")
    rule = get_promotion_rule("JPCP")
    assert rule["metric"] == "rmse"
    assert rule["threshold"] == 5.0


def test_set_baseline(db_path):
    from examlops.platform_db import init_db, set_drift_baseline, get_drift_baseline
    init_db()
    set_drift_baseline("JPCP", {"mean": 89.1, "std": 4.2, "n": 500})
    b = get_drift_baseline("JPCP")
    assert abs(b["mean"] - 89.1) < 0.001
