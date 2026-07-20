"""jpcp_config — Python shim for JPCP. Provides transforms only.

All declarative config (datasets, features, lifecycle, serving, prefect) lives in
pipelines/models/jpcp.yaml. This shim provides model-bound callables that require
a live model instance and cannot be expressed in YAML.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from seanergys_modelzoo.datasets.f_data import FDataDataset
from seanergys_modelzoo.datasets.pm100 import PM100Dataset
from seanergys_modelzoo.models.tasks.power_consumption_prediction.jpcp.jpcp_model import (
    JPCP,
    Embedding,
)


class JPCPConfiguration:
    """Python shim for JPCP — provides transform callables only.

    Does NOT inherit SeanergysModelConfiguration; that base class requires
    abstract methods now handled entirely by YAMLBackedConfig.
    """

    MODEL_CLASS: ClassVar[type] = JPCP
    SUPPORTED_DATASETS: ClassVar[list] = [PM100Dataset, FDataDataset]

    @classmethod
    def resolve_embedding_type(cls, value: str) -> Embedding:
        return Embedding[value]

    @classmethod
    def get_transforms(cls, model: JPCP, dataset_cls: type) -> dict:
        if dataset_cls is PM100Dataset:
            return {
                "preprocessing_functions": [model.transform_embeddings],
                "target_transform": lambda f: np.mean(f[0]) / f[1],
            }
        if dataset_cls is FDataDataset:
            return {
                "transform": model.embedding_parsing,
                "target_transform": lambda f: f[0] / f[1],
            }
        return {}
