"""Unit tests for Seanergys configurator (Pydantic params)."""

import inspect

import pytest
from pydantic import ValidationError
from torch.utils.data import DataLoader

from seanergys_modelzoo.models.common.seanergys_configurator import (
    SeanergysDataloaderParams,
    SeanergysDatasetParams,
    SeanergysModelParams,
)


# ---------------------------------------------------------------------------
# SeanergysComponentsParams (base)
# ---------------------------------------------------------------------------


def test_base_to_dict_and_from_dict_round_trip():
    """to_dict / from_dict are inverses of each other."""
    original = SeanergysDatasetParams(data_path="/tmp/data")
    restored = SeanergysDatasetParams.from_dict(original.to_dict())
    assert restored == original


def test_base_accepts_extra_fields():
    """extra='allow' lets callers attach arbitrary keys without error."""
    params = SeanergysDatasetParams(data_path="/tmp", custom_flag=True)
    assert params.to_dict()["custom_flag"] is True


# ---------------------------------------------------------------------------
# SeanergysModelParams
# ---------------------------------------------------------------------------


def test_model_params_empty_is_valid():
    """SeanergysModelParams requires no fields."""
    params = SeanergysModelParams()
    assert params.to_dict() == {}


def test_model_params_extra_fields_survive_round_trip():
    """Extra fields on SeanergysModelParams survive to_dict / from_dict."""
    params = SeanergysModelParams(hidden_dim=128, lr=0.001)
    restored = SeanergysModelParams.from_dict(params.to_dict())
    assert restored.to_dict() == {"hidden_dim": 128, "lr": 0.001}


# ---------------------------------------------------------------------------
# SeanergysDatasetParams
# ---------------------------------------------------------------------------


def test_dataset_params_all_fields_none_by_default():
    """All SeanergysDatasetParams fields default to None."""
    params = SeanergysDatasetParams()
    assert params.data_path is None
    assert params.transform is None
    assert params.target_transform is None
    assert params.metadata is None


def test_dataset_params_data_path_stored():
    """data_path is stored as provided."""
    params = SeanergysDatasetParams(data_path="/path/to/data")
    assert str(params.data_path) == "/path/to/data"


def test_dataset_params_from_dict():
    """from_dict reconstructs the object correctly."""
    params = SeanergysDatasetParams.from_dict({"data_path": "/tmp/data", "transform": None})
    assert str(params.data_path) == "/tmp/data"
    assert params.transform is None


# ---------------------------------------------------------------------------
# SeanergysDataloaderParams — contract / regression tests
# ---------------------------------------------------------------------------


def test_dataloader_params_contracts():
    """
    All field contracts for SeanergysDataloaderParams.
    These pin the agreed defaults so CI catches accidental changes.
    """
    params = SeanergysDataloaderParams()
    assert params.batch_size == 1
    assert params.shuffle is None
    assert params.sampler is None
    assert params.batch_sampler is None
    assert params.num_workers == 0
    assert params.collate_fn is None
    assert params.pin_memory is False
    assert params.drop_last is False
    assert params.timeout == 0
    assert params.worker_init_fn is None
    assert params.multiprocessing_context is None
    assert params.generator is None
    assert params.prefetch_factor is None
    assert params.persistent_workers is False
    assert params.pin_memory_device == ""
    assert params.in_order is True  # Seanergys-specific contract


def test_dataloader_params_to_dict_keys_match_pytorch_dataloader():
    """
    to_dict() keys must match PyTorch DataLoader's signature so that
    DataLoader(**params.to_dict()) works without extra filtering.
    """
    pytorch_keys = (
        set(inspect.signature(DataLoader.__init__).parameters) - {"self", "dataset"}
    )
    # in_order is Seanergys-only — exclude before comparing against PyTorch
    params_keys = set(SeanergysDataloaderParams().to_dict()) - {"in_order"}
    assert params_keys <= pytorch_keys, (
        f"Params not in DataLoader signature: {params_keys - pytorch_keys}"
    )


def test_dataloader_params_invalid_batch_size_type():
    """Non-integer batch_size raises a ValidationError."""
    with pytest.raises(ValidationError):
        SeanergysDataloaderParams(batch_size="not-an-int")


def test_dataloader_params_invalid_num_workers_type():
    """Non-integer num_workers raises a ValidationError."""
    with pytest.raises(ValidationError):
        SeanergysDataloaderParams(num_workers="many")


def test_dataloader_params_round_trip():
    """to_dict / from_dict preserves all scalar fields."""
    params = SeanergysDataloaderParams(batch_size=16, num_workers=4, drop_last=True, in_order=False)
    restored = SeanergysDataloaderParams.from_dict(params.to_dict())
    assert restored.batch_size == 16
    assert restored.num_workers == 4
    assert restored.drop_last is True
    assert restored.in_order is False
