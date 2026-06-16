"""demoanomaly_config — SeanergysModelConfiguration for DemoAnomaly.

DemoAnomaly trains on :class:`SyntheticAnomalyDataset` — in-code synthetic data,
no Zenodo / MinIO / dataplane download. The ``backend_name`` argument is accepted
for interface compatibility but ignored (synthetic data needs no storage backend).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader
from seanergys_modelzoo.datasets.synthetic_anomaly import SyntheticAnomalyDataset
from seanergys_modelzoo.models.common.seanergys_configurator import (
    SeanergysDataloaderParams,
    SeanergysDatasetParams,
    SeanergysModelConfiguration,
    SeanergysModelParams,
)
from seanergys_modelzoo.models.tasks.anomaly_detection.demoanomaly.demoanomaly_model import (
    DemoAnomaly,
)


def _identity_label(y):
    """Targets are already integer {0, 1} anomaly labels — pass through as int."""
    return int(y)


class DemoAnomalyConfiguration(SeanergysModelConfiguration):
    """Configuration for DemoAnomaly (IsolationForest on synthetic data)."""

    MODEL_CLASS: ClassVar[type] = DemoAnomaly
    SUPPORTED_DATASETS: ClassVar[list] = [SyntheticAnomalyDataset]

    @classmethod
    def get_transforms(cls, model: DemoAnomaly, dataset_cls: type) -> dict:
        return {
            "transform": model.embedding_parsing,
            "target_transform": _identity_label,
        }

    @classmethod
    def get_dummy_params(cls, dataset):
        return cls._build_params(dataset, is_dummy=True, split="train")

    @classmethod
    def get_training_params(cls, dataset):
        return cls._build_params(dataset, is_dummy=False, split="train")

    @classmethod
    def get_validation_params(cls, dataset):
        return cls._build_params(dataset, is_dummy=False, split="validation")

    @classmethod
    def get_testing_params(cls, dataset):
        return cls._build_params(dataset, is_dummy=False, split="test")

    @classmethod
    def get_train_components(
        cls,
        dataset_cls,
        split: str = "train",
        is_dummy: bool = False,
        backend_name: str | None = None,  # ignored — synthetic data has no backend
    ) -> tuple[DemoAnomaly, object, SeanergysDataloader]:
        """Return ready-to-use (model, dataset, loader) for a given split."""
        model_params, dataset_params, loader_params = cls._build_params(
            dataset_cls, is_dummy=is_dummy, split=split
        )
        model = DemoAnomaly(**model_params.to_dict())

        ds_kwargs = dataset_params.to_dict()
        ds_kwargs["transform"] = model.embedding_parsing

        dataset = SyntheticAnomalyDataset(**ds_kwargs)
        loader = SeanergysDataloader(dataset, **loader_params.to_dict())
        return model, dataset, loader

    @classmethod
    def get_inference_params(cls) -> dict:
        # ``lifecycle`` is the multi-stage promotion contract; thresholds are on
        # accuracy (higher_is_better). IsolationForest on the clean synthetic
        # mixture comfortably clears these.
        return {
            "model_id": "demoanomaly",
            "lifecycle": [
                {
                    "name": "Staging",
                    "metric": "accuracy",
                    "threshold": 0.70,
                    "direction": "higher_is_better",
                },
                {
                    "name": "Canary",
                    "metric": "accuracy",
                    "threshold": 0.80,
                    "direction": "higher_is_better",
                },
                {
                    "name": "Production",
                    "metric": "accuracy",
                    "threshold": 0.85,
                    "direction": "higher_is_better",
                },
            ],
            # Legacy single-stage gate (kept in sync with the Production rule).
            "promotion_metric": "accuracy",
            "promotion_threshold": 0.85,
            "promotion_direction": "higher_is_better",
            "input_schema": {"embedding": "list[float]"},
            "output_schema": {"is_anomaly": "int"},
        }

    @classmethod
    def _build_params(cls, dataset, is_dummy: bool, split: str):
        dataset_cls = dataset if isinstance(dataset, type) else type(dataset)
        if dataset_cls is SyntheticAnomalyDataset:
            return cls._synthetic_params(is_dummy, split)
        supported = [d.__name__ for d in cls.SUPPORTED_DATASETS]
        raise ValueError(f"Unsupported dataset: {dataset_cls.__name__}. Supported: {supported}")

    @classmethod
    def _synthetic_params(cls, is_dummy: bool, split: str):
        # contamination matches the anomaly fraction (100 / 500 = 0.2).
        model_params = SeanergysModelParams(
            model_hyperparameters={"contamination": 0.2, "random_state": 42, "n_jobs": -1}
        )
        dataset_params = SeanergysDatasetParams(
            transform=None,  # injected in get_train_components
            target_transform=_identity_label,
            # extra="allow" fields consumed by SyntheticAnomalyDataset:
            split=split,
            is_dummy=is_dummy,
            embedding_dim=384,
            n_normal=400,
            n_anomaly=100,
            seed=42,
        )
        loader_params = SeanergysDataloaderParams(batch_size=1)
        return model_params, dataset_params, loader_params
