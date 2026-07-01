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
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        tables = _get_tables(conn)
    assert {
        "audit_events",
        "drift_snapshots",
        "drift_baselines",
        "traffic_rules",
        "promotion_rules",
    } <= tables


def test_write_audit_event(db_path):
    from examlops.platform_db import get_db, init_db, write_audit_event

    init_db()
    write_audit_event("cli", "alice", "retrain_triggered", "JPCP", {"backend": "minio"})
    with get_db() as conn:
        row = conn.execute("SELECT source, actor, action, target FROM audit_events").fetchone()
    assert row["source"] == "cli"
    assert row["action"] == "retrain_triggered"


def test_write_drift_snapshot(db_path):
    from examlops.platform_db import get_db, init_db, write_drift_snapshot

    init_db()
    write_drift_snapshot("JPCP", "Production", 89.45, "job-abc")
    with get_db() as conn:
        row = conn.execute("SELECT model, alias, prediction FROM drift_snapshots").fetchone()
    assert row["model"] == "JPCP"
    assert abs(row["prediction"] - 89.45) < 0.001


def test_get_set_traffic_rules(db_path):
    from examlops.platform_db import get_traffic_rules, init_db, set_traffic_rules

    init_db()
    set_traffic_rules("JPCP", {"Production": 90, "Canary": 10}, "alice")
    rules = get_traffic_rules("JPCP")
    assert rules == {"Production": 90, "Canary": 10}


def test_get_traffic_rules_returns_none_if_missing(db_path):
    from examlops.platform_db import get_traffic_rules, init_db

    init_db()
    assert get_traffic_rules("JPCP") is None


def test_set_get_promotion_rule(db_path):
    from examlops.platform_db import get_promotion_rule, init_db, set_promotion_rule

    init_db()
    set_promotion_rule("JPCP", "rmse", "lt", 5.0, "Staging", "Production")
    rule = get_promotion_rule("JPCP")
    assert rule["metric"] == "rmse"
    assert rule["threshold"] == 5.0


def test_set_baseline(db_path):
    from examlops.platform_db import get_drift_baseline, init_db, set_drift_baseline

    init_db()
    set_drift_baseline("JPCP", {"mean": 89.1, "std": 4.2, "n": 500})
    b = get_drift_baseline("JPCP")
    assert abs(b["mean"] - 89.1) < 0.001


# ---------------------------------------------------------------------------
# Next-generation feature substrate (Phase 0 migration pass)
# ---------------------------------------------------------------------------

# All new tables from the 20-feature roadmap must exist after init_db().
NEXTGEN_TABLES = {
    "autoscale_config",
    "llm_endpoints",
    "feature_materializations",
    "data_versions",
    "data_contracts",
    "predictions",
    "ground_truth",
    "live_metrics",
    "label_queue",
    "ab_assignments",
    "canary_runs",
    "canary_steps",
    "eval_runs",
    "eval_results",
    "model_optimizations",
    "explanations",
    "fairness_reports",
    "fairness_gates",
    "attestations",
    "project_budgets",
    "carbon_records",
    "inference_energy",
    "compliance_records",
    "data_retention",
}


def test_init_db_creates_nextgen_tables(db_path):
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        tables = _get_tables(conn)
    assert NEXTGEN_TABLES <= tables


def test_init_db_is_idempotent(db_path):
    """init_db() runs on every process start; a second call (incl. the additive
    column migrations) must not error."""
    from examlops.platform_db import get_db, init_db

    init_db()
    init_db()  # must not raise (idempotent CREATE IF NOT EXISTS + guarded ALTER)
    with get_db() as conn:
        tables = _get_tables(conn)
    assert NEXTGEN_TABLES <= tables


def test_hpo_column_migration(db_path):
    """#8 HPO: additive columns land on the pre-existing hpo_studies/hpo_trials."""
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        study_cols = {r[1] for r in conn.execute("PRAGMA table_info(hpo_studies)").fetchall()}
        trial_cols = {r[1] for r in conn.execute("PRAGMA table_info(hpo_trials)").fetchall()}
    assert {"study_name", "sampler", "pruner", "state"} <= study_cols
    assert {"state", "pruned"} <= trial_cols


def test_autoscale_config_roundtrip(db_path):
    from examlops.platform_db import get_autoscale_config, init_db, set_autoscale_config

    init_db()
    assert get_autoscale_config("JPCP") is None
    set_autoscale_config("JPCP", 0, 6, target_ongoing=12, updated_by="alice")
    cfg = get_autoscale_config("JPCP")
    assert cfg["min_replicas"] == 0
    assert cfg["max_replicas"] == 6
    assert cfg["target_ongoing"] == 12


def test_feedback_loop_join(db_path):
    """#9 keystone: predictions join to delayed ground-truth via request_hash."""
    from examlops.platform_db import (
        init_db,
        join_predictions_with_truth,
        write_ground_truth,
        write_prediction,
    )

    init_db()
    write_prediction("JPCP", "Production", "hash-1", 90.0, job_id="job-a")
    write_prediction("JPCP", "Canary", "hash-2", 42.0, job_id="job-b")
    # only hash-1 gets a delayed label
    write_ground_truth("hash-1", 88.5, source="slurm")

    joined = join_predictions_with_truth("JPCP")
    assert len(joined) == 1
    assert joined[0]["request_hash"] == "hash-1"
    assert abs(joined[0]["prediction"] - 90.0) < 1e-6
    assert abs(joined[0]["label"] - 88.5) < 1e-6

    prod_only = join_predictions_with_truth("JPCP", alias="Canary")
    assert prod_only == []  # hash-2 has no label yet


def test_live_metrics_roundtrip(db_path):
    from examlops.platform_db import get_live_metrics, init_db, write_live_metric

    init_db()
    write_live_metric("JPCP", "Production", "rmse", 4.2, n=500)
    rows = get_live_metrics("JPCP", alias="Production")
    assert len(rows) == 1
    assert rows[0]["metric"] == "rmse"
    assert rows[0]["n"] == 500


def test_fairness_gate_roundtrip(db_path):
    from examlops.platform_db import get_fairness_gates, init_db, set_fairness_gate

    init_db()
    set_fairness_gate("JPCP", "pclass", "demographic_parity", 0.1)
    gates = get_fairness_gates("JPCP")
    assert len(gates) == 1
    assert gates[0]["max_disparity"] == 0.1


def test_project_budget_and_carbon(db_path):
    from examlops.platform_db import (
        get_db,
        get_project_budget,
        init_db,
        set_project_budget,
        write_carbon_record,
    )

    init_db()
    set_project_budget("eu-hpc", gpu_hours_budget=1000.0, cost_budget=5000.0)
    b = get_project_budget("eu-hpc")
    assert b["gpu_hours_budget"] == 1000.0
    write_carbon_record("JPCP", "run-1", kwh=3.5, co2e_g=812.0, grid_intensity=232.0)
    with get_db() as conn:
        row = conn.execute("SELECT model, kwh, co2e_g FROM carbon_records").fetchone()
    assert row["model"] == "JPCP"
    assert abs(row["co2e_g"] - 812.0) < 1e-6
