"""ADR 0017 clause 2 — the feature gate is on the real training path, not only in its own module.

The static half runs everywhere; the behavioural half imports the pipeline engine and so needs the
use-case pack's model library (skipped without it, like the other flow tests).
"""

from __future__ import annotations

import ast
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PG = _ROOT / "pipelines" / "pipeline_generator.py"


def _training_flow_calls() -> set[str]:
    tree = ast.parse(_PG.read_text())
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "training_flow"
    )
    return {
        c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    }


def test_training_flow_runs_the_feature_gate_and_tags_the_run():
    calls = _training_flow_calls()
    assert "_feature_gate" in calls, "training_flow no longer runs the ADR 0017 feature gate"
    assert "tag_run" in calls, "training_flow no longer records which feature view trained it"


def _pg():
    mz = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (_ROOT / "modelzoo"))
    if not (mz / "seanergys_modelzoo").is_dir():
        pytest.skip("seanergys_modelzoo not present")
    for p in (str(_ROOT), str(mz), str(_ROOT / "platform" / "cli" / "src")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import pipelines.pipeline_generator as pg

    return pg


def test_the_bound_view_is_enforced_against_the_run_data(tmp_path, monkeypatch):
    pg = _pg()
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_FEATURES_DIR", str(_ROOT / "usecases" / "reference" / "features"))
    from pipelines.feature_gate import FeatureViewViolation

    yaml_cfg = MagicMock()
    yaml_cfg.dataset.return_value = types.SimpleNamespace(feature_view="fdata_job_features")
    cfg = MagicMock()
    cfg._yaml = yaml_cfg
    bad = pd.DataFrame({"job_id": ["j1"], "adt": ["2024-01-01"], "embedding": [[0.0] * 10]})
    sample = types.SimpleNamespace(frame=bad, rows=1, total_rows=1, sampled=False)
    with (
        patch.dict(pg.MODEL_REGISTRY, {"GATED": (MagicMock(), cfg, {})}),
        patch.object(pg, "_contract_inputs", lambda *a, **k: ([("", lambda: sample)], "/x")),
    ):
        assert pg._feature_view_binding("GATED", "FDataDataset") == "fdata_job_features"
        with pytest.raises(FeatureViewViolation, match="expected 384 dims, got 10"):
            pg._feature_gate("GATED", "FDataDataset", None, False)
        skipped = pg._feature_gate("GATED", "FDataDataset", None, True)
    assert skipped["validated"] is False and "dummy" in skipped["reason"]
    assert pg._feature_view_binding("UNKNOWN", "FDataDataset") is None


def test_the_gate_reads_only_the_rows_it_checks_not_the_whole_dataset(tmp_path, monkeypatch):
    """The feature gate checks EXAMLOPS_FEATURE_GATE_MAX_ROWS rows; it must not first load every
    parquet file of the dataset into the orchestrator (F-DATA is ~117k rows x 384 floats a file)."""
    pg = _pg()
    for i, n in enumerate((3, 4)):
        pd.DataFrame({"k": [f"{i}-{j}" for j in range(n)], "v": list(range(n))}).to_parquet(
            tmp_path / f"part{i}.parquet"
        )
    monkeypatch.setattr(
        "pipelines.datasets.versioning.resolve_revision",
        lambda *_a, **_k: types.SimpleNamespace(uri=str(tmp_path)),
    )
    whole, _ = pg._contract_dataframe("D", None)
    head, _ = pg._contract_dataframe("D", None, max_rows=5)
    pd.testing.assert_frame_equal(head, whole.head(5))

    def refuse(*_a, **_k):
        raise AssertionError("the bounded path read a whole file with pd.read_parquet")

    monkeypatch.setattr(pd, "read_parquet", refuse)
    assert len(pg._contract_dataframe("D", None, max_rows=2)[0]) == 2

    seen: dict = {}

    def capture(_dataset, _backend, **kw):
        seen.update(kw)
        return None, "stop"

    monkeypatch.setenv("EXAMLOPS_FEATURE_GATE_MAX_ROWS", "7")
    monkeypatch.setenv("EXAMLOPS_FEATURES_DIR", str(_ROOT / "usecases" / "reference" / "features"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    yaml_cfg = MagicMock()
    yaml_cfg.dataset.return_value = types.SimpleNamespace(feature_view="fdata_job_features")
    cfg = MagicMock()
    cfg._yaml = yaml_cfg
    with (
        patch.dict(pg.MODEL_REGISTRY, {"GATED": (MagicMock(), cfg, {})}),
        patch.object(pg, "_contract_inputs", capture),
    ):
        report = pg._feature_gate("GATED", "FDataDataset", None, False)
    assert seen.get("max_rows") == 7 and report["reason"] == "stop"
