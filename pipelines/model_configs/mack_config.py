"""mack_config — Python shim for MACK. Provides transforms only.

All declarative config lives in pipelines/models/mack.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from seanergys_modelzoo.datasets.f_data import FDataDataset
from seanergys_modelzoo.models.tasks.performance_prediction.mack.mack_model import (
    MACK,
    Embedding,
)

_PCLASS_MAP = {"memory-bound": 0.0, "compute-bound": 1.0}


def _pclass_target_transform(y: np.ndarray) -> float:
    return _PCLASS_MAP.get(str(y[0]), -1.0)


class MACKConfiguration:
    """Python shim for MACK — provides transform callables only."""

    MODEL_CLASS: ClassVar[type] = MACK
    SUPPORTED_DATASETS: ClassVar[list] = [FDataDataset]

    @classmethod
    def resolve_embedding_type(cls, value: str) -> Embedding:
        return Embedding[value]

    @classmethod
    def get_transforms(cls, model: MACK, dataset_cls: type) -> dict:
        return {
            "transform": model.embedding_parsing,
            "target_transform": _pclass_target_transform,
        }
