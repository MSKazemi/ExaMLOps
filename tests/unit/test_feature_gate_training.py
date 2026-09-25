"""ADR 0017 clauses 2 + 3, training half — the feature gate on the real training path."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
sys.path.insert(0, str(ROOT))

VIEW = """\
name: jobs
entity: job
entity_key: job_id
timestamp_field: adt
features:
  - {name: embedding, dtype: vector, dim: 3}
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in (
        "EXAMLOPS_FEATURE_GATE",
        "EXAMLOPS_FEATURE_GATE_MAX_ROWS",
        "EXAMLOPS_FEATURE_GATE_INGEST",
        "EXAMLOPS_FEATURE_ONLINE_STORE",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops.platform_db import init_db

    init_db()


@pytest.fixture
def defs(tmp_path) -> str:
    d = tmp_path / "features"
    d.mkdir()
    (d / "jobs.yaml").write_text(VIEW)
    return str(d)


def _loader(frame: pd.DataFrame, table: str = ""):
    sample = types.SimpleNamespace(
        frame=frame, rows=len(frame), total_rows=len(frame), sampled=False
    )
    return lambda: ([(table, lambda: sample)], "/data/pinned")


def _frame(rows=3, dim=3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "job_id": [f"j{i}" for i in range(rows)],
            "adt": pd.to_datetime([f"2024-01-0{i + 1}" for i in range(rows)]),
            "embedding": [[float(i)] * dim for i in range(rows)],
            "avgpcon": [1.0] * rows,
        }
    )


def _gate(defs, frame, **kw):
    from pipelines.feature_gate import feature_view_gate

    return feature_view_gate(
        "FData", view_name="jobs", definitions_dir=defs, load_inputs=_loader(frame), **kw
    )


def test_valid_training_data_passes_and_lands_in_the_offline_store(defs):
    from examlops import feature_store as fs

    report = _gate(defs, _frame())
    assert report["passed"] is True and report["validated"] is True
    assert report["rows_checked"] == 3 and report["row_failures"] == 0
    assert report["offline_written"] == 3 and report["pit_mismatches"] == 0
    # Point-in-time retrieval now serves exactly what the model trained on.
    got = fs.get_training_features("jobs", [{"entity_id": "j2", "event_ts": "2024-01-03 00:00:00"}])
    assert got == [{"embedding": [2.0, 2.0, 2.0]}]
    # And the view was mirrored into the registry from the pack file.
    assert fs.get_view("jobs").spec["fingerprint"] == report["fingerprint"]


def test_rerunning_over_the_same_pinned_data_is_idempotent(defs):
    _gate(defs, _frame())
    again = _gate(defs, _frame())
    assert again["offline_written"] == 0 and again["offline_skipped"] == 3
    from examlops.platform_db import get_db

    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM feature_records WHERE view='jobs'").fetchone()[0]
    assert n == 3


def test_rows_serving_would_reject_fail_the_run_closed(defs):
    from pipelines.feature_gate import FeatureViewViolation

    with pytest.raises(FeatureViewViolation, match="3/3 training rows violate .*expected 3 dims"):
        _gate(defs, _frame(dim=4))


def test_warn_mode_reports_and_continues(defs, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_FEATURE_GATE", "warn")
    report = _gate(defs, _frame(dim=4))
    assert report["passed"] is False and "expected 3 dims" in report["failure"]


def test_a_missing_feature_column_fails_closed(defs):
    from pipelines.feature_gate import FeatureViewViolation

    with pytest.raises(FeatureViewViolation, match=r"lacks feature column\(s\) \['embedding'\]"):
        _gate(defs, _frame().drop(columns=["embedding"]))


def test_an_in_data_conflict_is_ambiguous_kept_out_of_the_store_and_counted(defs):
    """Real F-DATA carries the same jid + adt with different embeddings (2044 such groups in
    21_04_full.parquet). No retrieval can be point-in-time correct for one of those, so the gate
    must neither store an arbitrary value (it would later be served as the truth) nor fail every
    FData training run on a property of the upstream dataset."""
    from examlops.platform_db import get_db

    frame = _frame(rows=3)
    frame.loc[1, "job_id"] = "j0"
    frame.loc[1, "adt"] = frame.loc[0, "adt"]
    report = _gate(defs, frame)
    assert report["passed"] is True and report["pit"] == "checked"
    assert report["pit_ambiguous"] == 1 and report["offline_written"] == 1
    with get_db() as conn:
        ids = [
            r[0] for r in conn.execute("SELECT entity_id FROM feature_records WHERE view='jobs'")
        ]
    assert ids == ["j2"]


def test_an_identical_duplicate_row_is_one_observation_not_a_conflict(defs):
    frame = pd.concat([_frame(rows=2), _frame(rows=1)], ignore_index=True)
    report = _gate(defs, frame)
    assert report["passed"] is True and "pit_ambiguous" not in report
    assert report["offline_written"] == 2


def test_a_conflict_with_what_the_offline_store_already_holds_fails_closed(defs):
    """The store (which feeds materialization, hence serving) holds a different value for the
    same entity + event time than this run trains on: that is train/serve skew, so it refuses."""
    from pipelines.feature_gate import FeatureViewViolation

    _gate(defs, _frame())
    changed = _frame()
    changed.at[1, "embedding"] = [9.0, 9.0, 9.0]
    with pytest.raises(FeatureViewViolation, match="already holds a different value"):
        _gate(defs, changed)


def test_the_entity_column_may_differ_from_the_request_field(tmp_path):
    """FData names the job `jid`; requests name it `job_id`. The gate must key on the column."""
    d = tmp_path / "f"
    d.mkdir()
    (d / "jobs.yaml").write_text(
        VIEW.replace("entity_key: job_id\n", "entity_key: job_id\nentity_column: jid\n")
    )
    frame = _frame().rename(columns={"job_id": "jid"})
    report = _gate(str(d), frame)
    assert report["pit"] == "checked" and report["offline_written"] == 3


def test_missing_point_in_time_columns_are_reported_not_silently_passed(defs):
    report = _gate(defs, _frame().rename(columns={"job_id": "jid"}))
    assert report["passed"] is True  # the feature check itself passed
    assert report["pit"].startswith("skipped:") and "'job_id'/'adt'" in report["pit"]
    assert "offline_written" not in report


def test_zone_aware_event_times_are_stored_in_utc(defs):
    from examlops import feature_store as fs

    frame = _frame(rows=1)
    frame["adt"] = ["2021-04-01 10:28:01+09"]  # F-DATA's own spelling
    _gate(defs, frame)
    assert fs.get_training_features(
        "jobs", [{"entity_id": "j0", "event_ts": "2021-04-01 01:28:01"}]
    ) == [{"embedding": [0.0, 0.0, 0.0]}]
    # Nine hours earlier than that is before the event: nothing may leak back.
    assert fs.get_training_features(
        "jobs", [{"entity_id": "j0", "event_ts": "2021-04-01 01:28:00"}]
    ) == [None]


def test_the_reference_view_keys_on_fdatas_real_columns():
    """The shipped view must name the columns F-DATA's parquet really has (jid, adt), or clause 3
    silently never runs on the one production view."""
    from examlops.feature_store.definitions import load_definitions

    view = load_definitions(ROOT / "usecases" / "reference" / "features").views[
        "fdata_job_features"
    ]
    assert view.dataset_entity_column == "jid" and view.timestamp_field == "adt"
    assert view.entity_key == "job_id"  # what an inference request carries


def test_ingest_is_one_transaction_not_one_per_row(defs, monkeypatch):
    from examlops.data import data_assets

    opened = []
    real = data_assets.get_db

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(data_assets, "get_db", counting)
    from examlops import feature_store as fs

    rows = [
        {"entity_id": f"e{i}", "event_ts": "2024-01-01 00:00:00", "values": {"embedding": [1.0]}}
        for i in range(50)
    ]
    assert fs.ingest_rows("jobs", rows) == {"written": 50, "skipped": 0}
    fs.get_training_features(
        "jobs", [{"entity_id": r["entity_id"], "event_ts": r["event_ts"]} for r in rows]
    )
    assert len(opened) <= 4, f"{len(opened)} connections for 50 rows"


def test_row_cap_bounds_the_work(defs, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_FEATURE_GATE_MAX_ROWS", "2")
    report = _gate(defs, _frame(rows=5))
    assert report["rows_checked"] == 2 and report["offline_written"] == 2


def test_ingestion_can_be_disabled(defs, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_FEATURE_GATE_INGEST", "0")
    report = _gate(defs, _frame())
    assert report["passed"] is True and "offline_written" not in report


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"view_name": None}, "no feature view bound"),
        ({"is_dummy": True}, "dummy run"),
    ],
)
def test_what_it_cannot_check_is_a_skip_with_a_reason(defs, kw, reason):
    from pipelines.feature_gate import feature_view_gate

    args = {"view_name": "jobs", "definitions_dir": defs, "load_inputs": _loader(_frame())}
    report = feature_view_gate("FData", **{**args, **kw})
    assert report["validated"] is False and reason in report["reason"]


def test_an_unreadable_location_is_a_skip_not_a_pass(defs):
    from pipelines.feature_gate import feature_view_gate

    report = feature_view_gate(
        "FData",
        view_name="jobs",
        definitions_dir=defs,
        load_inputs=lambda: (None, "revision uri is not a readable local path"),
    )
    assert report["validated"] is False and "readable" in report["reason"]


def test_a_binding_to_an_undeclared_view_fails_closed(defs):
    from pipelines.feature_gate import FeatureViewViolation, feature_view_gate

    with pytest.raises(FeatureViewViolation, match="'ghost' is not declared"):
        feature_view_gate(
            "FData", view_name="ghost", definitions_dir=defs, load_inputs=_loader(_frame())
        )


def test_gate_off_does_nothing(defs, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_FEATURE_GATE", "off")
    assert _gate(defs, _frame(dim=9))["reason"] == "gate disabled"


def test_the_run_is_tagged_with_the_view_that_trained_it(monkeypatch):
    from pipelines.feature_gate import tag_run

    tags: dict = {}

    class FakeClient:
        def set_tag(self, run_id, key, value):
            tags[(run_id, key)] = value

    fake = types.ModuleType("mlflow.tracking")
    fake.MlflowClient = FakeClient
    monkeypatch.setitem(sys.modules, "mlflow.tracking", fake)
    passed = {"view": "jobs", "fingerprint": "abc", "validated": True, "passed": True}
    assert tag_run("run-1", passed) is True
    assert tags == {
        ("run-1", "feature_view"): "jobs",
        ("run-1", "feature_view_fingerprint"): "abc",
        ("run-1", "feature_view_validated"): "passed",
    }
    # A skipped gate still binds the run to the definition, but must not read as a checked one.
    skipped = {"view": "jobs", "fingerprint": "abc", "validated": False, "reason": "dummy run"}
    assert tag_run("run-2", skipped) is True
    assert tags[("run-2", "feature_view_validated")] == "skipped: dummy run"
    warned = {"view": "jobs", "fingerprint": "abc", "validated": True, "passed": False}
    assert tag_run("run-3", warned) is True
    assert tags[("run-3", "feature_view_validated")] == "failed"
    assert tag_run("run-1", {"view": "jobs"}) is False  # unbound/skipped gate: nothing to tag
    assert tag_run(None, {"view": "jobs", "fingerprint": "abc"}) is False


def test_the_model_yaml_binding_is_parsed():
    from pipelines.model_loader import load_model_yaml

    cfg = load_model_yaml(ROOT / "usecases" / "reference" / "models" / "jpcp.yaml")
    assert cfg.dataset("FDataDataset").feature_view == "fdata_job_features"
    assert cfg.dataset("PM100Dataset").feature_view is None
