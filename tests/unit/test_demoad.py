"""Unit tests for DemoAD — auto-scaffolded.

These tests don't hit Zenodo / MinIO / dataplane. They verify that the
config registers the model, the param bundles build, and the model class
instantiates cleanly with dummy hyperparameters.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELZOO = REPO_ROOT / "modelzoo"
for p in (str(REPO_ROOT), str(MODELZOO)):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_demoad_is_registered():
    from pipelines.pipeline_generator import MODEL_REGISTRY

    assert "DemoAD" in MODEL_REGISTRY, (
        "DemoAD should auto-register via its config; "
        f"registry has: {list(MODEL_REGISTRY.keys())}"
    )


def test_demoad_config_is_loadable():
    from pipelines.model_configs.demoad_config import DemoADConfiguration

    cls = DemoADConfiguration.MODEL_CLASS
    assert cls is not None
    assert DemoADConfiguration.SUPPORTED_DATASETS  # at least one dataset


def test_demoad_inference_params_have_required_keys():
    from pipelines.model_configs.demoad_config import DemoADConfiguration

    inf = DemoADConfiguration.get_inference_params()
    for key in ("model_id", "promotion_metric", "promotion_threshold", "promotion_direction"):
        assert key in inf, f"missing inference key: {key}"


def test_demoad_instantiates():
    from seanergys_modelzoo.models.tasks.power_consumption_prediction.demoad.demoad_model import (
        DemoAD,
    )

    model = DemoAD(model_hyperparameters={"n_jobs": -1})
    assert model is not None
    assert model.task_type is not None
