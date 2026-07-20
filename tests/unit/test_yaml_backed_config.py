"""Tests for YAMLBackedConfig — the YAML-driven drop-in for SeanergysModelConfiguration."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if p not in sys.path:
        sys.path.insert(0, p)

from pipelines.usecase import models_dir  # noqa: E402

# Resolve the active use-case pack's model dir (ADR 0094), not the pre-migration in-tree path.
MODELS_DIR = models_dir()


def _make_backed(model_name: str):
    from pipelines.model_loader import load_model_yaml
    from pipelines.pipeline_generator import YAMLBackedConfig, _import_shim

    yaml_cfg = load_model_yaml(MODELS_DIR / f"{model_name.lower()}.yaml")
    shim = _import_shim(yaml_cfg.config_class)
    return YAMLBackedConfig(yaml_cfg, shim)


def test_jpcp_inference_params_model_id():
    backed = _make_backed("jpcp")
    params = backed.get_inference_params()
    assert params["model_id"] == "jpcp"


def test_jpcp_inference_params_lifecycle():
    backed = _make_backed("jpcp")
    params = backed.get_inference_params()
    assert len(params["lifecycle"]) == 3
    assert params["promotion_metric"] == "rmse"
    assert params["promotion_threshold"] == 50.0
    assert params["promotion_direction"] == "lower_is_better"


def test_jpcp_inference_params_legacy_keys():
    backed = _make_backed("jpcp")
    params = backed.get_inference_params()
    for key in ("promotion_metric", "promotion_threshold", "promotion_direction", "model_id"):
        assert key in params, f"Missing legacy key: {key}"


def test_mack_inference_params_classification():
    backed = _make_backed("mack")
    params = backed.get_inference_params()
    assert params["promotion_metric"] == "accuracy"
    assert params["promotion_direction"] == "higher_is_better"
    assert params["promotion_threshold"] == 0.70


def test_jpcp_supported_datasets_has_two():
    backed = _make_backed("jpcp")
    names = {d.__name__ for d in backed.SUPPORTED_DATASETS}
    assert "PM100Dataset" in names
    assert "FDataDataset" in names


def test_mack_supported_datasets_has_one():
    backed = _make_backed("mack")
    assert len(backed.SUPPORTED_DATASETS) == 1
    assert backed.SUPPORTED_DATASETS[0].__name__ == "FDataDataset"


def test_model_class_is_correct():
    from pipelines.model_loader import load_model_yaml
    from pipelines.pipeline_generator import _import_shim

    yaml_cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    shim = _import_shim(yaml_cfg.config_class)
    backed_cls = shim.MODEL_CLASS
    assert backed_cls.__name__ == "JPCP"


def test_get_train_components_signature_accepts_backend():
    import inspect

    backed = _make_backed("jpcp")
    sig = inspect.signature(backed.get_train_components)
    assert "backend_name" in sig.parameters


def test_parse_filter_value_utc_timestamp():
    """UTC ISO 8601 date strings must produce timezone-aware Timestamps."""
    import pandas as pd

    from pipelines.pipeline_generator import _parse_filter_value

    ts = _parse_filter_value("2020-05-01T00:00:00+00:00")
    assert isinstance(ts, pd.Timestamp)
    assert ts.tz is not None  # timezone-aware


def test_parse_filter_value_plain_date():
    """Plain date strings (no T) must stay as str — FData parquet stores date columns as string."""
    from pipelines.pipeline_generator import _parse_filter_value

    result = _parse_filter_value("2023-12-01")
    assert isinstance(result, str)
    assert result == "2023-12-01"


def test_parse_filter_value_non_date_passthrough():
    from pipelines.pipeline_generator import _parse_filter_value

    assert _parse_filter_value(">=") == ">="
    assert _parse_filter_value(42) == 42


def test_yaml_backed_config_has_name_attribute():
    """YAMLBackedConfig must expose __name__ for pipeline error messages."""
    backed = _make_backed("jpcp")
    assert hasattr(backed, "__name__")
    assert "JPCP" in backed.__name__


def test_get_inference_params_empty_lifecycle():
    """Empty lifecycle should not crash; promotion fields get safe defaults."""
    from pipelines.model_loader import load_model_yaml
    from pipelines.pipeline_generator import YAMLBackedConfig, _import_shim

    yaml_cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    shim = _import_shim(yaml_cfg.config_class)
    backed = YAMLBackedConfig(yaml_cfg, shim)
    # Temporarily empty the lifecycle
    original = backed._yaml.lifecycle
    backed._yaml.lifecycle = []
    try:
        params = backed.get_inference_params()
        assert params["promotion_metric"] == ""
        assert params["promotion_threshold"] == 0.0
    finally:
        backed._yaml.lifecycle = original
