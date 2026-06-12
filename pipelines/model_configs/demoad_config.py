"""demoad_config — Auto-scaffolded SeanergysModelConfiguration for DemoAD."""

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
from seanergys_modelzoo.datasets._backends import get_backend
from seanergys_modelzoo.datasets.f_data import FDataDataset
from seanergys_modelzoo.models.common.seanergys_configurator import (
    SeanergysDataloaderParams,
    SeanergysDatasetParams,
    SeanergysModelConfiguration,
    SeanergysModelParams,
)
from seanergys_modelzoo.models.tasks.power_consumption_prediction.demoad.demoad_model import DemoAD

# Regression target — scalar reduction from raw output features.


def _scalar_target_transform(f):
    """Map raw target tuple to a single scalar (override for your model)."""
    import numpy as np

    return float(np.mean(f) if hasattr(f, "__iter__") else f)


class DemoADConfiguration(SeanergysModelConfiguration):
    """Configuration for DemoAD — TODO: one-line description."""

    MODEL_CLASS: ClassVar[type] = DemoAD
    SUPPORTED_DATASETS: ClassVar[list] = [FDataDataset]

    @classmethod
    def get_transforms(cls, model: DemoAD, dataset_cls: type) -> dict:
        return {
            "transform": model.embedding_parsing,
            "target_transform": _scalar_target_transform,
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
        backend_name: str | None = None,
    ) -> tuple[DemoAD, object, SeanergysDataloader]:
        """Return ready-to-use (model, dataset, loader) for a given split."""
        model_params, dataset_params, loader_params = cls._build_params(
            dataset_cls, is_dummy=is_dummy, split=split
        )
        model = DemoAD(**model_params.to_dict())

        ds_kwargs = dataset_params.to_dict()
        ds_kwargs["transform"] = model.embedding_parsing

        backend = get_backend(backend_name)
        if backend is not None:
            ds_kwargs["backend"] = backend
            ds_kwargs.pop("use_zenodo_url", None)

        dataset = FDataDataset(**ds_kwargs)
        loader = SeanergysDataloader(dataset, **loader_params.to_dict())
        return model, dataset, loader

    @classmethod
    def get_inference_params(cls) -> dict:
        # Phase 3: ``lifecycle`` is the multi-stage promotion contract.
        # Tune the per-stage thresholds for your model — the defaults below
        # form a safe Staging -> Canary -> Production progression.
        return {
            "model_id": "demoad",
            "lifecycle": [
                {
                    "name": "Staging",
                    "metric": "accuracy",
                    "threshold": 0.7,
                    "direction": "higher_is_better",
                },
                {
                    "name": "Canary",
                    "metric": "accuracy",
                    "threshold": 0.7,
                    "direction": "higher_is_better",
                },
                {
                    "name": "Production",
                    "metric": "accuracy",
                    "threshold": 0.7,
                    "direction": "higher_is_better",
                },
            ],
            # Legacy single-stage gate (kept in sync with the Production rule).
            "promotion_metric": "accuracy",
            "promotion_threshold": 0.7,
            "promotion_direction": "higher_is_better",
            "input_schema": {"embedding": "list[float]"},
            "output_schema": {"avgpcon": "float"},
        }

    @classmethod
    def _build_params(cls, dataset, is_dummy: bool, split: str):
        dataset_cls = dataset if isinstance(dataset, type) else type(dataset)
        if dataset_cls is FDataDataset:
            return cls._fdata_params(is_dummy, split)
        supported = [d.__name__ for d in cls.SUPPORTED_DATASETS]
        raise ValueError(f"Unsupported dataset: {dataset_cls.__name__}. Supported: {supported}")

    @classmethod
    def _fdata_params(cls, is_dummy: bool, split: str):
        model_params = SeanergysModelParams(model_hyperparameters={"n_jobs": -1})
        if split == "train":
            files = ["23_12", "24_01"]
            filters = [("adt", ">=", "2023-12-01"), ("adt", "<=", "2024-01-31")]
        else:
            files = ["24_02"]
            filters = [("adt", ">=", "2024-02-01"), ("adt", "<=", "2024-02-28")]
        _cache = Path(__file__).resolve().parents[2] / ".data_cache" / "fdata"
        dataset_params = SeanergysDatasetParams(
            use_zenodo_url=True,
            is_dummy=is_dummy,
            files=files,
            transform=None,  # injected in get_train_components
            target_transform=_scalar_target_transform,
            filters=filters,
            input_features=["embedding"],
            output_features=["avgpcon"],
            download_path=str(_cache),
        )
        loader_params = SeanergysDataloaderParams(batch_size=1)
        return model_params, dataset_params, loader_params
