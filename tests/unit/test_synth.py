"""A7 — Synthetic data generation (ADR 0042).

GWT acceptance criteria from ``design/vision/specs/A7-synthetic-data-generation.md`` §5:

* GWT-1 schema — generated records match the real schema.
* GWT-2 fidelity gate — a low-fidelity synthetic set is not released.
* GWT-3 privacy gate — a memorizing generator is flagged and blocked.
* GWT-4 provenance — a released synthetic dataset is an A1 revision flagged ``synthetic=true``.
* GWT-5 policy — a synthetic-only training set is detectable so promotion can be forbidden.

All tests run offline in the pure-python fallback path (SDV is an optional extra).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _real_frame(n: int = 400, seed: int = 42) -> pd.DataFrame:
    """A representative HPC-telemetry frame: correlated numerics, a categorical, an int,
    and a list-valued embedding column (like FData)."""
    rng = np.random.default_rng(seed)
    x = rng.normal(10.0, 3.0, n)
    return pd.DataFrame(
        {
            "cpu": x,
            "mem": x * 0.8 + rng.normal(0.0, 1.0, n),
            "pclass": rng.choice(["memory-bound", "compute-bound"], n, p=[0.3, 0.7]),
            "count": rng.integers(0, 100, n),
            "embedding": [list(rng.normal(0, 1, 4)) for _ in range(n)],
        }
    )


# --------------------------------------------------------------------------- GWT-1


def test_gwt1_schema_match():
    from examlops.synth import synth_fit, synth_generate

    real = _real_frame()
    synth = synth_fit("rev_src", "gaussian_copula", data=real, seed=1)
    ds = synth_generate(synth, 300, seed=1)
    assert list(ds.data.columns) == list(real.columns)
    assert ds.data["count"].dtype == real["count"].dtype  # int stays int
    assert ds.data["cpu"].dtype == real["cpu"].dtype  # float stays float
    assert len(ds.data) == 300
    # list column preserved as lists (bootstrap), never coerced away
    assert isinstance(ds.data["embedding"].iloc[0], list)


def test_gwt1_generated_values_within_real_range():
    from examlops.synth import synth_fit, synth_generate

    real = _real_frame()
    ds = synth_generate(synth_fit("r", "gaussian_copula", data=real, seed=2), 300, seed=2)
    # Inverse-quantile sampling never extrapolates beyond the observed support.
    assert ds.data["cpu"].min() >= real["cpu"].min() - 1e-6
    assert ds.data["cpu"].max() <= real["cpu"].max() + 1e-6
    assert set(ds.data["pclass"].unique()) <= set(real["pclass"].unique())


# --------------------------------------------------------------------------- GWT-2


def test_gwt2_low_fidelity_blocked():
    from examlops.synth import synth_evaluate

    real = _real_frame()
    rng = np.random.default_rng(7)
    noise = pd.DataFrame(
        {
            "cpu": rng.uniform(-100, 100, 300),
            "mem": rng.uniform(-100, 100, 300),
            "pclass": ["unseen"] * 300,
            "count": rng.integers(500, 600, 300),
            "embedding": [[0, 0, 0, 0]] * 300,
        }
    )
    gate = synth_evaluate(real, noise)
    assert gate["released"] is False
    assert gate["fidelity"]["score"] < 0.6
    assert any("fidelity" in r for r in gate["reasons"])


# --------------------------------------------------------------------------- GWT-3


def test_gwt3_memorizing_generator_blocked():
    from examlops.synth import synth_evaluate

    real = _real_frame()
    # A generator that memorizes = emits exact copies of real records.
    gate = synth_evaluate(real, real.copy())
    assert gate["released"] is False
    assert gate["privacy"]["score"] < 0.5
    assert gate["privacy"]["exact_match_fraction"] == pytest.approx(1.0)
    assert any("privacy" in r or "memoris" in r for r in gate["reasons"])


def test_gwt3_partial_memorization_penalized():
    from examlops.synth import synth_evaluate, synth_fit, synth_generate

    real = _real_frame()
    good = synth_generate(synth_fit("r", "gaussian_copula", data=real, seed=3), 300, seed=3).data
    # Half real copies, half genuinely synthetic → privacy must drop vs the clean set.
    mixed = pd.concat([real.iloc[:150], good.iloc[:150]], ignore_index=True)
    clean_priv = synth_evaluate(real, good)["privacy"]["score"]
    mixed_priv = synth_evaluate(real, mixed)["privacy"]["score"]
    assert mixed_priv < clean_priv


# --------------------------------------------------------------------------- GWT-4


def test_gwt4_released_synthetic_is_flagged_a1_revision():
    from examlops.data.data_assets import (
        get_dataset_revision,
        get_synthetic_dataset,
        record_dataset_revision,
        record_synthetic_dataset,
    )
    from examlops.synth import synth_evaluate, synth_fit, synth_generate

    real = _real_frame()
    synth = synth_fit("rev_source", "gaussian_copula", data=real, seed=1)
    ds = synth_generate(synth, 300, seed=1)
    gate = synth_evaluate(real, ds.data)
    assert gate["released"] is True  # a clean copula pass releases

    from pipelines.datasets.versioning import DatasetRevision

    rev = DatasetRevision(
        backend="synthetic", dataset="FData", revision_id=ds.revision_id, kind="synthetic"
    )
    record_dataset_revision(
        rev,
        row_count=ds.n_rows,
        synthetic=True,
        source_revision="rev_source",
        generator="gaussian_copula",
    )
    record_synthetic_dataset(
        ds.revision_id,
        "FData",
        source_revision="rev_source",
        method="gaussian_copula",
        params=ds.params,
        n_rows=ds.n_rows,
        fidelity_score=gate["fidelity"]["score"],
        privacy_score=gate["privacy"]["score"],
        released=True,
    )

    row = get_dataset_revision("FData", ds.revision_id)
    assert row is not None
    assert row["synthetic"] == 1  # hard flag → can never pass as real (R4)
    assert row["source_revision"] == "rev_source"
    assert row["generator"] == "gaussian_copula"

    prov = get_synthetic_dataset(ds.revision_id)
    assert prov is not None and prov["released"] is True
    assert prov["fidelity_score"] == gate["fidelity"]["score"]
    assert prov["source_revision"] == "rev_source"


def test_gwt4_record_dataset_revision_defaults_real():
    """Backward compatibility: existing callers get synthetic=0 with no new args."""
    from examlops.data.data_assets import get_dataset_revision, record_dataset_revision
    from pipelines.datasets.versioning import DatasetRevision

    rev = DatasetRevision(backend="minio", dataset="FData", revision_id="abc123", kind="content")
    record_dataset_revision(rev)  # no synthetic kwargs
    row = get_dataset_revision("FData", "abc123")
    assert row["synthetic"] == 0


# --------------------------------------------------------------------------- GWT-5


def test_gwt5_is_synthetic_only_and_proportion():
    from examlops.data.data_assets import (
        is_synthetic_only,
        record_dataset_revision,
        synthetic_proportion,
    )
    from pipelines.datasets.versioning import DatasetRevision

    real = DatasetRevision(backend="minio", dataset="FData", revision_id="real1", kind="content")
    syn = DatasetRevision(
        backend="synthetic", dataset="FData", revision_id="syn1", kind="synthetic"
    )
    record_dataset_revision(real)
    record_dataset_revision(syn, synthetic=True, source_revision="real1", generator="tvae")

    assert is_synthetic_only(["syn1"]) is True
    assert is_synthetic_only(["real1", "syn1"]) is False
    assert is_synthetic_only([]) is False
    assert synthetic_proportion(["real1", "syn1"]) == pytest.approx(0.5)
    assert synthetic_proportion(["syn1"]) == pytest.approx(1.0)
    # Unknown revision ids count as real (conservative).
    assert synthetic_proportion(["ghost"]) == pytest.approx(0.0)


# --------------------------------------------------------------------- fallback/core


def test_fallback_backend_when_sdv_absent():
    from examlops.synth import synth_fit

    real = _real_frame()
    synth = synth_fit("r", "gaussian_copula", data=real, seed=1)
    assert synth.backend == "fallback"  # SDV not installed in CI → graceful degradation
    assert set(synth.numeric_cols) == {"cpu", "mem", "count"}
    assert synth.categorical_cols == ["pclass"]
    assert synth.other_cols == ["embedding"]


def test_generation_is_deterministic():
    from examlops.synth import synth_fit, synth_generate

    real = _real_frame()
    synth = synth_fit("r", "gaussian_copula", data=real, seed=5)
    a = synth_generate(synth, 200, seed=5)
    b = synth_generate(synth, 200, seed=5)
    assert a.revision_id == b.revision_id
    pd.testing.assert_frame_equal(a.data, b.data)


def test_normal_synthetic_passes_gate():
    from examlops.synth import synth_evaluate, synth_fit, synth_generate

    real = _real_frame()
    ds = synth_generate(synth_fit("r", "gaussian_copula", data=real, seed=1), 400, seed=1)
    gate = synth_evaluate(real, ds.data)
    assert gate["released"] is True
    assert 0.0 <= gate["fidelity"]["score"] <= 1.0
    assert 0.0 <= gate["privacy"]["score"] <= 1.0
    assert gate["fidelity"]["correlation_delta"] is not None  # correlation term computed


def test_fit_rejects_unknown_method_and_empty():
    from examlops.synth import synth_fit

    with pytest.raises(ValueError):
        synth_fit("r", "not_a_method", data=_real_frame())
    with pytest.raises(ValueError):
        synth_fit("r", "gaussian_copula", data=pd.DataFrame())


def test_gate_fails_closed_on_metric_error(monkeypatch):
    """Security: if metric computation raises, the gate BLOCKS (never releases unvetted data)."""
    from examlops.synth import gate as gate_mod

    def _boom(*a, **k):
        raise RuntimeError("metric exploded")

    monkeypatch.setattr(gate_mod, "fidelity_metrics", _boom)
    result = gate_mod.evaluate_and_gate(_real_frame(50), _real_frame(50))
    assert result["released"] is False
    assert any("evaluation error" in r for r in result["reasons"])


def test_single_numeric_column_generates():
    """A one-numeric-column frame exercises the scalar-correlation copula path."""
    from examlops.synth import synth_fit, synth_generate

    real = pd.DataFrame({"x": np.random.default_rng(0).normal(0, 1, 100)})
    ds = synth_generate(synth_fit("r", "gaussian_copula", data=real, seed=1), 50, seed=1)
    assert list(ds.data.columns) == ["x"]
    assert len(ds.data) == 50


def test_constant_column_is_handled():
    """A constant numeric column (std 0) must not crash the copula fit/sample."""
    from examlops.synth import synth_fit, synth_generate

    real = pd.DataFrame({"const": [5.0] * 80, "v": np.random.default_rng(1).normal(0, 1, 80)})
    ds = synth_generate(synth_fit("r", "gaussian_copula", data=real, seed=2), 40, seed=2)
    assert set(ds.data["const"].unique()) == {5.0}


def test_metrics_scores_bounded():
    from examlops.synth.metrics import fidelity_metrics, privacy_metrics

    real = _real_frame()
    for score in (fidelity_metrics(real, real)["score"], privacy_metrics(real, real)["score"]):
        assert 0.0 <= score <= 1.0
    # identical frames → perfect fidelity, worst privacy (full memorization)
    assert fidelity_metrics(real, real)["score"] == pytest.approx(1.0)
    assert privacy_metrics(real, real)["score"] == pytest.approx(0.0)


# ------------------------------------------------------------------------------ CLI


def _write_parquet(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def test_cli_generate_releases_and_records_provenance(tmp_path):
    from examlops.cli.commands import synth_cmd
    from examlops.data.data_assets import get_dataset_revisions, is_synthetic_only

    real_path = _write_parquet(_real_frame(), tmp_path / "real" / "fdata.parquet")
    out_dir = tmp_path / "synth"
    res = runner.invoke(
        synth_cmd.app,
        [
            "generate",
            "FData",
            "--path",
            str(real_path),
            "--rows",
            "300",
            "--out",
            str(out_dir),
            "--seed",
            "1",
        ],
    )
    assert res.exit_code == 0, res.output
    revs = get_dataset_revisions("FData", "synthetic")
    assert len(revs) == 1
    assert revs[0]["synthetic"] == 1
    assert revs[0]["generator"] == "gaussian_copula"
    assert is_synthetic_only([revs[0]["revision_id"]]) is True
    # a parquet was materialized under --out
    assert list(out_dir.glob("*.parquet"))


def test_cli_evaluate_fails_on_memorization(tmp_path):
    from examlops.cli.commands import synth_cmd

    real = _real_frame()
    real_path = _write_parquet(real, tmp_path / "real" / "fdata.parquet")
    memo_path = _write_parquet(real.copy(), tmp_path / "memo" / "fdata.parquet")
    res = runner.invoke(
        synth_cmd.app,
        ["evaluate", "FData", "--real", str(real_path), "--synthetic", str(memo_path)],
    )
    assert res.exit_code == 1  # gate blocks memorized data


def test_cli_generate_blocks_low_fidelity(tmp_path):
    from examlops.cli.commands import synth_cmd
    from examlops.data.data_assets import get_synthetic_dataset

    # Real data that is nearly constant → a copula of it cannot match a high min-fidelity
    # threshold set absurdly high, forcing a block; provenance is still recorded (blocked).
    real = _real_frame()
    real_path = _write_parquet(real, tmp_path / "real" / "fdata.parquet")
    res = runner.invoke(
        synth_cmd.app,
        [
            "generate",
            "FData",
            "--path",
            str(real_path),
            "--rows",
            "200",
            "--min-fidelity",
            "0.999",
            "--min-privacy",
            "0.999",
            "--seed",
            "1",
        ],
    )
    assert res.exit_code == 1  # blocked by the impossible thresholds
    # The blocked attempt is still recorded (released=0) for auditability.
    from examlops.data.data_assets import list_synthetic_datasets

    recs = list_synthetic_datasets("FData")
    assert len(recs) == 1 and recs[0]["released"] is False
    assert get_synthetic_dataset(recs[0]["revision_id"])["released"] is False
