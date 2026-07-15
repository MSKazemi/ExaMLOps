# Future Pydantic Tests

Tests that should be added to `tests/unit/` to cover pydantic validation capabilities
already present in the codebase but not yet tested.

---

## The Pattern

```python
# valid input → object is created → assert fields are correct
def test_X_valid():
    obj = MyClass(field="good_value")
    assert obj.field == "good_value"

# invalid input → must raise ValidationError immediately
def test_X_invalid():
    with pytest.raises(ValidationError):
        MyClass(field="bad_value")
```

---

## 1. Required Fields

Missing required field must raise `ValidationError` immediately.

```python
from pydantic import ValidationError
from seanergys_modelzoo.models.common.seanergys_model_metadata import SeanergysModelMetadata

def test_metadata_name_is_required():
    with pytest.raises(ValidationError):
        SeanergysModelMetadata()              # no name → must fail

def test_metadata_name_is_accepted():
    m = SeanergysModelMetadata(name="mack_model")
    assert m.name == "mack_model"
```

---

## 2. Type Validation

Wrong type must raise `ValidationError` immediately.

```python
from pydantic import ValidationError
from seanergys_modelzoo.models.common.seanergys_configurator import SeanergysDataloaderParams

def test_batch_size_wrong_type_rejected():
    with pytest.raises(ValidationError):
        SeanergysDataloaderParams(batch_size="ten")   # str instead of int → must fail

def test_batch_size_correct_type_accepted():
    params = SeanergysDataloaderParams(batch_size=32)
    assert params.batch_size == 32
```

---

## 3. `model_validator(mode='after')` — Cross-field Validation

Runs after the object is built. Checks relationships between fields.

```python
from pydantic import ValidationError
from seanergys_modelzoo.datasets.common.seanergys_parquet_dataset import SeanergysParquetDataset

def test_parquet_dataset_validator_after_valid():
    ds = SeanergysParquetDataset(
        input_features=["cpu", "memory"],
        output_features=["power"]
    )
    assert ds.input_features == ["cpu", "memory"]

def test_parquet_dataset_validator_after_empty_features():
    with pytest.raises(ValidationError):
        SeanergysParquetDataset(
            input_features=[],        # empty → validator must reject
            output_features=["power"]
        )
```

---

## 4. `model_validator(mode='before')` — Raw Input Validation

Runs before fields are set. Catches bad raw input before pydantic even parses it.

```python
from seanergys_modelzoo.models.tasks.performance_prediction.mack.mack_model import MackModel

def test_mack_model_validator_before_valid():
    model = MackModel(model_hyperparameters={"n_estimators": 2, "n_jobs": 1})
    assert model is not None

def test_mack_model_validator_before_invalid():
    with pytest.raises((ValidationError, TypeError)):
        MackModel(model_hyperparameters="not_a_dict")  # must fail before fields are set
```

---

## 5. `default_factory` — Mutable Defaults Are Isolated

Two instances must not share the same mutable object.

```python
def test_dataset_stats_are_isolated():
    ds1 = SyntheticDataset()
    ds2 = SyntheticDataset()
    assert ds1.stats is not ds2.stats   # separate dict objects
```

---

## 6. `extra='allow'` — Unknown Fields Are Accepted

```python
def test_dataloader_params_accepts_extra_fields():
    params = SeanergysDataloaderParams(batch_size=4, unknown_field="x")
    assert params.unknown_field == "x"
```

---

## 7. `Union[str, Path]` — Accepts Both Types

```python
from pathlib import Path
from seanergys_modelzoo.models.common.seanergys_configurator import SeanergysDatasetParams

def test_data_path_accepts_string():
    params = SeanergysDatasetParams(data_path="/tmp/data")
    assert params.data_path == "/tmp/data"

def test_data_path_accepts_path_object():
    params = SeanergysDatasetParams(data_path=Path("/tmp/data"))
    assert params.data_path == Path("/tmp/data")
```

---

## 8. Non-Empty List Fields

`input_features` and `output_features` must not be empty.

```python
def test_parquet_dataset_output_features_not_empty():
    with pytest.raises(ValidationError):
        SeanergysParquetDataset(
            input_features=["cpu"],
            output_features=[]        # empty → must fail
        )
```

---

## Summary

| # | Capability | Source file | Priority |
|---|-----------|-------------|----------|
| 1 | Required fields | `seanergys_model_metadata.py` | High |
| 2 | Type validation | `seanergys_configurator.py` | High |
| 3 | `model_validator` after | `seanergys_parquet_dataset.py` | High |
| 4 | `model_validator` before | `mack_model.py`, `mcbound_model.py`, `jpcp_model.py` | High |
| 5 | `default_factory` isolation | `seanergys_dataset.py` | Medium |
| 6 | `extra='allow'` | `seanergys_configurator.py` | Low |
| 7 | `Union[str, Path]` | `seanergys_configurator.py` | Low |
| 8 | Non-empty list fields | `seanergys_parquet_dataset.py` | Medium |
