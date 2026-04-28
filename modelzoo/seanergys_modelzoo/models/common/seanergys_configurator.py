# Changed by Mohsen: added 'from __future__ import annotations' for Python 3.12
# compatibility (PEP 563 — lazy evaluation of annotations avoids NameError on
# forward references and PEP 604 / PEP 585 union/generic syntax at class body time).
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
)

from pydantic import BaseModel, ConfigDict, Field
from torch.utils.data.sampler import Sampler

from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset


# ---------------------------------------------------------------------------
# Base params
# ---------------------------------------------------------------------------


class SeanergysComponentsParams(BaseModel):
    """
    Base Pydantic configuration class for all Seanergys components.

    Accepts arbitrary extra fields so that subclasses and callers can freely
    extend the parameter set without modifying this base class.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,  # Required for torch Sampler, Callable, etc.
        extra="allow",  # Mirrors the original **kwargs catch-all
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the config to a plain dictionary."""
        return self.model_dump()

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "SeanergysComponentsParams":
        """Deserialise the config from a plain dictionary."""
        return cls(**config_dict)


# ---------------------------------------------------------------------------
# Model params
# ---------------------------------------------------------------------------


class SeanergysModelParams(SeanergysComponentsParams):
    """
    Configuration parameters for a SeanergysModel instantiation.

    Subclass this and declare typed fields for every hyper-parameter your
    model exposes. Undeclared keys are accepted via extra="allow".
    """

    pass


# ---------------------------------------------------------------------------
# Dataset params
# ---------------------------------------------------------------------------


class SeanergysDatasetParams(SeanergysComponentsParams):
    """Configuration parameters for a SeanergysDataset."""

    data_path: Optional[Union[str, Path]] = Field(
        default=None,
        description="Path to the raw dataset on disk",
    )
    transform: Optional[Any] = Field(
        default=None,
        description="Input transform applied to each sample",
    )
    target_transform: Optional[Any] = Field(
        default=None,
        description="Transform applied to each target/label",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Arbitrary dataset-level metadata",
    )


# ---------------------------------------------------------------------------
# Dataloader params
# ---------------------------------------------------------------------------


class SeanergysDataloaderParams(SeanergysComponentsParams):
    """
    Configuration parameters for a SeanergysDataloader.

    Field names and defaults mirror PyTorch's DataLoader signature exactly so
    that instances can be unpacked directly into DataLoader(**params.to_dict()).
    """

    batch_size: Optional[int] = Field(
        default=1,
        description="Number of samples per batch",
    )
    shuffle: Optional[bool] = Field(
        default=None,
        description="Shuffle data at every epoch",
    )
    sampler: Optional[Union[Sampler, Iterable]] = Field(
        default=None,
        description="Custom sampler; mutually exclusive with shuffle",
    )
    batch_sampler: Optional[Union[Sampler[list], Iterable[list]]] = Field(
        default=None,
        description="Returns a batch of indices at a time; overrides batch_size, shuffle, sampler, drop_last",
    )
    num_workers: int = Field(
        default=0,
        description="Subprocesses to use for data loading (0 = main process)",
    )
    collate_fn: Optional[Callable[[list], Any]] = Field(
        default=None,
        description="Merges a list of samples into a mini-batch tensor",
    )
    pin_memory: bool = Field(
        default=False,
        description="Copy tensors into pinned memory before returning",
    )
    drop_last: bool = Field(
        default=False,
        description="Drop the last incomplete batch",
    )
    timeout: float = Field(
        default=0,
        description="Timeout for collecting a batch from workers (seconds)",
    )
    worker_init_fn: Optional[Callable[[int], None]] = Field(
        default=None,
        description="Called on each worker subprocess with the worker id",
    )
    multiprocessing_context: Optional[Any] = Field(
        default=None,
        description="Multiprocessing start method or context",
    )
    generator: Optional[Any] = Field(
        default=None,
        description="torch.Generator used to seed workers",
    )
    prefetch_factor: Optional[int] = Field(
        default=None,
        description="Batches loaded in advance per worker",
    )
    persistent_workers: bool = Field(
        default=False,
        description="Keep worker processes alive between epochs",
    )
    pin_memory_device: str = Field(
        default="",
        description="Device to pin memory to when pin_memory=True",
    )
    in_order: bool = Field(
        default=True,
        description="Seanergys-specific flag: preserve sample order across batches",
    )


# ---------------------------------------------------------------------------
# Model configuration (abstract)
# ---------------------------------------------------------------------------


class 
(BaseModel, ABC):
    """
    Abstract configurator that binds a SeanergysModel to one or more
    SeanergysDatasets via fully typed parameter bundles.

    For each concrete SeanergysModel, create one subclass of this class and
    implement get_training_params, get_testing_params, and get_validation_params
    for every dataset the model supports.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    # Changed by Mohsen: was 'Field(default_factory=list)' (Pydantic instance field),
    # which caused Pydantic to treat it as a model field instead of a class constant.
    # ClassVar is required so subclasses can override it at the class level and so
    # pipeline code can read it without instantiating the configurator.
    SUPPORTED_DATASETS: ClassVar[List[Any]] = []

    # Declare MODEL_CLASS in every concrete subclass so the pipeline engine can
    # auto-discover and register model+config pairs without any manual wiring.
    # Example:  MODEL_CLASS: ClassVar[Type] = JPCP
    MODEL_CLASS: ClassVar[Any] = None

    @classmethod
    @abstractmethod
    def get_dummy_params(
        cls,
        dataset: "SeanergysDataset",
    ) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        """
        Return the full parameter bundle for dummy testing on the given dataset.

        Args:
            dataset: The dataset instance (or class) to configure training for.

        Returns:
            A tuple of (model_params, dataset_params, dataloader_params).
        """
        pass

    @classmethod
    @abstractmethod
    def get_training_params(
        cls,
        dataset: "SeanergysDataset",
    ) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        """
        Return the full parameter bundle for training on the given dataset.

        Args:
            dataset: The dataset instance (or class) to configure training for.

        Returns:
            A tuple of (model_params, dataset_params, dataloader_params).
        """
        pass

    @classmethod
    @abstractmethod
    def get_testing_params(
        cls,
        dataset: "SeanergysDataset",
    ) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        """
        Return the full parameter bundle for testing on the given dataset.

        Args:
            dataset: The dataset instance (or class) to configure testing for.

        Returns:
            A tuple of (model_params, dataset_params, dataloader_params).
        """
        pass

    @classmethod
    @abstractmethod
    def get_validation_params(
        cls,
        dataset: "SeanergysDataset",
    ) -> Tuple[SeanergysModelParams, SeanergysDatasetParams, SeanergysDataloaderParams]:
        """
        Return the full parameter bundle for validation on the given dataset.

        Args:
            dataset: The dataset instance (or class) to configure validation for.

        Returns:
            A tuple of (model_params, dataset_params, dataloader_params).
        """
        pass

    # Changed by Mohsen: was an instance method (self), but SUPPORTED_DATASETS is a
    # ClassVar so it must be accessed via the class, not an instance.
    @classmethod
    def get_dataset_classes(cls) -> List:
        """Return the list of dataset classes this configuration supports."""
        return cls.SUPPORTED_DATASETS
