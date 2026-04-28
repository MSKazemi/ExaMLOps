"""
jpcp_config — Concrete SeanergysModelConfiguration for JPCP.

Binds JPCP to PM100Dataset and FDataDataset with typed parameter bundles
for every split (train / validation / test / dummy).

Pipeline additions over the base framework:
  - get_train_components(): builds ready-to-use (model, dataset, loader)
    instances, handling model-dependent preprocessing (embeddings, transforms).
  - get_inference_params(): MLflow model_id + promotion gate + I/O schema.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar, List, Tuple

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader
from seanergys_modelzoo.datasets.f_data import FDataDataset
from seanergys_modelzoo.datasets.pm100 import PM100Dataset
from seanergys_modelzoo.models.common.seanergys_configurator import (
    SeanergysDataloaderParams,
    SeanergysDatasetParams,
    SeanergysModelConfiguration,
    SeanergysModelParams,
)
from seanergys_modelzoo.models.tasks.power_consumption_prediction.jpcp.jpcp_model import (
    JPCP,
    Embedding,
)


class JPCPConfiguration(SeanergysModelConfiguration):
    """Configuration for JPCP — power consumption prediction on HPC data."""

    MODEL_CLASS: ClassVar[type] = JPCP
    SUPPORTED_DATASETS: ClassVar[List] = [PM100Dataset, FDataDataset]

    # ── Abstract method implementations ───────────────────────────────────────

    @classmethod
    def get_dummy_params(cls, dataset) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        return cls._build_params(dataset, is_dummy=True, split="train")

    @classmethod
    def get_training_params(cls, dataset) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        return cls._build_params(dataset, is_dummy=False, split="train")

    @classmethod
    def get_validation_params(cls, dataset) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        return cls._build_params(dataset, is_dummy=False, split="validation")

    @classmethod
    def get_testing_params(cls, dataset) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        return cls._build_params(dataset, is_dummy=False, split="test")

    # ── Component builder ──────────────────────────────────────────────────────

    @classmethod
    def get_train_components(
        cls,
        dataset_cls,
        split: str = "train",
        is_dummy: bool = False,
    ) -> Tuple[JPCP, object, SeanergysDataloader]:
        """Return ready-to-use (model, dataset, loader) for a given split.

        Handles model-dependent preprocessing (embeddings, transforms) that
        cannot be stored in serializable params.
        """
        model_params, dataset_params, loader_params = cls._build_params(
            dataset_cls, is_dummy=is_dummy, split=split
        )
        model = JPCP(**model_params.to_dict())

        ds_kwargs = dataset_params.to_dict()
        # Inject model-bound callables after the model exists
        if dataset_cls is PM100Dataset:
            ds_kwargs["preprocessing_functions"] = [model.transform_embeddings]
        elif dataset_cls is FDataDataset:
            ds_kwargs["transform"] = model.embedding_parsing

        dataset = dataset_cls(**ds_kwargs)
        loader = SeanergysDataloader(dataset, **loader_params.to_dict())
        return model, dataset, loader

    @classmethod
    def get_inference_params(cls) -> dict:
        """Inference metadata used by the pipeline generator.

        model_id          → MLflow registered model name
        promotion_metric  → which evaluation metric gates Production promotion
        promotion_threshold / direction → promotion condition
        input_schema / output_schema → Ray Serve API contract
        """
        return {
            "model_id": "jpcp",
            "promotion_metric": "rmse",
            "promotion_threshold": 50.0,
            "promotion_direction": "lower_is_better",
            "input_schema": {
                "num_nodes_req": "int",
                "user_id": "str",
            },
            "output_schema": {
                "power_per_node_watts": "float",
            },
        }

    # ── Internal helpers ───────────────────────────────────────────────────────

    @classmethod
    def _build_params(cls, dataset, is_dummy: bool, split: str):
        dataset_cls = dataset if isinstance(dataset, type) else type(dataset)
        if dataset_cls is PM100Dataset:
            return cls._pm100_params(is_dummy, split)
        if dataset_cls is FDataDataset:
            return cls._fdata_params(is_dummy, split)
        supported = [d.__name__ for d in cls.SUPPORTED_DATASETS]
        raise ValueError(f"Unsupported dataset: {dataset_cls.__name__}. Supported: {supported}")

    @classmethod
    def _pm100_params(cls, is_dummy: bool, split: str):
        model_params = SeanergysModelParams(
            embedding_type=Embedding.INT,
            model_hyperparameters={"n_jobs": -1},
        )
        if split == "train":
            filters = [
                ("submit_time", ">=", pd.Timestamp("2020-05-01", tz="UTC")),
                ("submit_time", "<=", pd.Timestamp("2020-09-01", tz="UTC")),
            ]
        else:
            filters = [
                ("submit_time", ">=", pd.Timestamp("2020-09-01", tz="UTC")),
                ("submit_time", "<=", pd.Timestamp("2020-10-01", tz="UTC")),
            ]
        _cache = Path(__file__).resolve().parents[2] / ".data_cache" / "pm100"
        dataset_params = SeanergysDatasetParams(
            use_zenodo_url=True,
            is_dummy=is_dummy,
            transform=None,
            target_transform=lambda f: np.mean(f[0]) / f[1],
            filters=filters,
            columns=["num_nodes_req", "user_id", "node_power_consumption", "num_nodes_alloc"],
            input_features=["num_nodes_req_cat", "user_id_cat"],
            output_features=["node_power_consumption", "num_nodes_alloc"],
            download_path=str(_cache),
        )
        loader_params = SeanergysDataloaderParams(batch_size=1)
        return model_params, dataset_params, loader_params

    @classmethod
    def _fdata_params(cls, is_dummy: bool, split: str):
        model_params = SeanergysModelParams(
            embedding_type=Embedding.NONE,
            model_hyperparameters={"n_jobs": -1},
        )
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
            transform=None,
            target_transform=lambda f: f[0] / f[1],
            filters=filters,
            input_features=["embedding"],
            output_features=["avgpcon", "nnuma"],
            download_path=str(_cache),
        )
        loader_params = SeanergysDataloaderParams(batch_size=1)
        return model_params, dataset_params, loader_params
