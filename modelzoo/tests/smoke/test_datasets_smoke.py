"""
Smoke tests: dataset discovery, contract validation, and data loading.

SECTIONS
--------

1. Discovery       — at least one dataset/model class found. No deps, no I/O.
2. Contract checks — dataset and model interfaces look correct. No data loaded.
3. Fixtures        — load fixture parquet from tests/fixtures/sample_data/.
                     No model config needed. Driven by datasets that declare
                     ZENODO_URL or ZENODO_BASE_URL. Skips if fixture missing
                     (run `make sample-data` first).
4. is_dummy        — load via model get_train_config() + is_dummy=True.
                     Marked @zenodo (is_dummy=True still hits Zenodo with filters).
                     Skip with: CI_SKIP_ZENODO=1 make test-smoke
5. Zenodo          — load directly from Zenodo URL with is_dummy=True.
                     Marked @zenodo. Requires network.
                     Skip with: CI_SKIP_ZENODO=1 make test-smoke

Sections 3 is dataset-driven (no model needed).
Sections 4 and 5 are model-driven via get_train_config().
No dataset or model names are hardcoded anywhere.
"""

import os
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
pytest.importorskip("torch")
pytest.importorskip("sklearn")

from tests.conftest import DATASET_CLASSES, MODEL_CLASSES

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sample_data"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _zenodo_dataset_classes() -> dict:
    """
    Return dataset classes that declare ZENODO_URL or ZENODO_BASE_URL as a
    Pydantic field. These can be sampled independently of any model config.
    """
    return {
        name: cls
        for name, cls in DATASET_CLASSES.items()
        if "ZENODO_URL" in cls.model_fields or "ZENODO_BASE_URL" in cls.model_fields
    }


_ZENODO_DATASET_CLASSES = _zenodo_dataset_classes()

def _collect_dataset_configs_from_models():
    """
    Collect unique (dataset_cls, ds_params) from all model get_train_config() calls.
    Deduplicated by dataset class name so each dataset is loaded only once.
    """
    seen = {}
    for model_name, model_cls in MODEL_CLASSES.items():
        try:
            config = model_cls.get_train_config()
        except Exception:
            continue
        for entry_name, (_, dataset_cls, (ds_params, _dl)) in config.items():
            key = dataset_cls.__name__
            if key not in seen:
                seen[key] = (dataset_cls, ds_params, f"{model_name}/{entry_name}")
    return seen


_DATASET_CONFIGS_FROM_MODELS = _collect_dataset_configs_from_models()


# ---------------------------------------------------------------------------
# 1. Discovery
# ---------------------------------------------------------------------------

def test_dataset_discovery():
    """
    PASS: at least one SeanergysDataset subclass found in datasets/.
    FAIL: no dataset classes found — check that your dataset file is in
          seanergys_modelzoo/datasets/ and subclasses SeanergysDataset.
    """
    assert len(DATASET_CLASSES) >= 1, (
        "No SeanergysDataset subclasses found in seanergys_modelzoo/datasets/.\n"
        "Make sure your dataset file:\n"
        "  - lives in seanergys_modelzoo/datasets/\n"
        "  - subclasses SeanergysDataset\n"
        "  - is not named __init__.py"
    )


def test_model_discovery():
    """
    PASS: at least one SeanergysModel subclass found in models/tasks/.
    FAIL: no model classes found — check that your model file is in
          seanergys_modelzoo/models/tasks/ and subclasses SeanergysModel.
    """
    assert len(MODEL_CLASSES) >= 1, (
        "No SeanergysModel subclasses found in seanergys_modelzoo/models/tasks/.\n"
        "Make sure your model file:\n"
        "  - lives in seanergys_modelzoo/models/tasks/<task>/<name>/\n"
        "  - subclasses SeanergysModel\n"
        "  - is not named __init__.py"
    )


# ---------------------------------------------------------------------------
# 2. Contract checks (no data loaded)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "dataset_name,config",
    list(_DATASET_CONFIGS_FROM_MODELS.items()),
    ids=list(_DATASET_CONFIGS_FROM_MODELS.keys()),
)
def test_dataset_contract_via_model_config(dataset_name, config):
    """
    Contract check — model get_train_config() shape only, no loading.

    PASS: model provides a valid dataset class with non-empty features defined.
    SKIP: no model references this dataset via get_train_config().
    FAIL: dataset_cls or ds_params missing required fields.
    """
    dataset_cls, ds_params, source = config

    assert dataset_cls is not None, (
        f"FAIL [{dataset_name}] dataset_cls from {source} is None."
    )
    assert hasattr(ds_params, "input_features"), (
        f"FAIL [{dataset_name}] ds_params from {source} missing input_features."
    )
    assert hasattr(ds_params, "output_features"), (
        f"FAIL [{dataset_name}] ds_params from {source} missing output_features."
    )
    assert len(ds_params.input_features) >= 1, (
        f"FAIL [{dataset_name}] ds_params.input_features from {source} is empty."
    )
    assert len(ds_params.output_features) >= 1, (
        f"FAIL [{dataset_name}] ds_params.output_features from {source} is empty."
    )


# ---------------------------------------------------------------------------
# 3. Fixtures — load from tests/fixtures/sample_data/ (no model config)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "dataset_cls",
    list(_ZENODO_DATASET_CLASSES.values()),
    ids=list(_ZENODO_DATASET_CLASSES.keys()),
)
def test_dataset_fixture_has_data(dataset_cls):
    """
    Load the fixture parquet for each Zenodo-capable dataset and verify it
    has rows and columns. No model config, no network, no feature lists needed.

    PASS: fixture parquet loads, has at least 1 row and 1 column.
    SKIP: fixture file not found — run `make sample-data` to create it.
    FAIL: fixture loads but is empty.

    Fixture naming: FDataDataset → sample_fdata.parquet
    """
    dataset_name = dataset_cls.__name__
    name = dataset_name.lower().replace("dataset", "")
    fixture = _FIXTURES_DIR / f"sample_{name}.parquet"

    if not fixture.exists():
        pytest.skip(
            f"SKIP [{dataset_name}] fixture not found: {fixture.name}\n"
            f"  → Run `make sample-data` to create it."
        )

    df = pd.read_parquet(fixture)

    assert len(df) >= 1, (
        f"FAIL [{dataset_name}] fixture {fixture.name} has 0 rows."
    )
    assert len(df.columns) >= 1, (
        f"FAIL [{dataset_name}] fixture {fixture.name} has 0 columns."
    )


# ---------------------------------------------------------------------------
# 4. is_dummy — load via model get_train_config() + is_dummy=True (Zenodo)
# ---------------------------------------------------------------------------

@pytest.mark.zenodo
@pytest.mark.parametrize(
    "dataset_name,config",
    list(_DATASET_CONFIGS_FROM_MODELS.items()),
    ids=list(_DATASET_CONFIGS_FROM_MODELS.keys()),
)
def test_dataset_loads_with_is_dummy(dataset_name, config):
    """
    Load dataset using params from model get_train_config() + is_dummy=True.

    PASS: dataset instantiates with model-provided params, loads data, len > 0,
          dataset[0] returns a valid (x, y) tuple.
    SKIP: CI_SKIP_ZENODO=1 set, or no model references this dataset.
    FAIL: dataset loads 0 rows, or dataset[0] fails.

    Marked @zenodo because is_dummy=True still downloads from Zenodo with filters.
    Skip with: CI_SKIP_ZENODO=1 make test-smoke
    """
    if os.environ.get("CI_SKIP_ZENODO") == "1":
        pytest.skip("SKIP: CI_SKIP_ZENODO=1 — Zenodo tests disabled for this run.")

    dataset_cls, ds_params, source = config

    params = ds_params.model_dump()
    params["is_dummy"] = True

    try:
        dataset = dataset_cls(**params)
    except Exception as e:
        pytest.fail(
            f"FAIL [{dataset_name}] failed to instantiate using params "
            f"from {source}.\n  → Error: {e}"
        )

    assert len(dataset) >= 1, (
        f"FAIL [{dataset_name}] loaded 0 samples with is_dummy=True.\n"
        f"  → Params came from {source}.\n"
        f"  → Check that is_dummy=True reduces data but still returns at least 1 row."
    )

    item = dataset[0]
    assert item is not None, (
        f"FAIL [{dataset_name}] dataset[0] returned None.\n"
        f"  → Check that {dataset_name}.__getitem__() returns a valid (x, y) tuple."
    )


# ---------------------------------------------------------------------------
# 5. Zenodo — load directly from Zenodo URL with is_dummy=True
# ---------------------------------------------------------------------------

@pytest.mark.zenodo
@pytest.mark.parametrize(
    "dataset_name,config",
    list(_DATASET_CONFIGS_FROM_MODELS.items()),
    ids=list(_DATASET_CONFIGS_FROM_MODELS.keys()),
)
def test_dataset_loads_from_zenodo(dataset_name, config):
    """
    Load each dataset from its Zenodo source with is_dummy=True (small subset).

    PASS: dataset downloads from Zenodo, loads data, len > 0, dataset[0] valid.
    SKIP: CI_SKIP_ZENODO=1 set, or dataset does not support use_zenodo_url=True.
    FAIL: Zenodo download succeeds but 0 rows loaded, or dataset[0] fails.

    Requires network. Skip locally with: CI_SKIP_ZENODO=1 make test-smoke
    """
    if os.environ.get("CI_SKIP_ZENODO") == "1":
        pytest.skip("SKIP: CI_SKIP_ZENODO=1 — Zenodo tests disabled for this run.")

    dataset_cls, ds_params, source = config

    params = ds_params.model_dump()
    params["is_dummy"] = True
    params["use_zenodo_url"] = True

    try:
        dataset = dataset_cls(**params)
    except TypeError:
        pytest.skip(
            f"\n  SKIP [{dataset_name}] use_zenodo_url=True not supported.\n"
            f"  → Add use_zenodo_url field to {dataset_name} to enable Zenodo testing."
        )
    except Exception as e:
        pytest.fail(
            f"FAIL [{dataset_name}] Zenodo download or load failed.\n"
            f"  → Error: {e}"
        )

    assert len(dataset) >= 1, (
        f"FAIL [{dataset_name}] loaded 0 samples from Zenodo with is_dummy=True.\n"
        f"  → Check that is_dummy=True filters the Zenodo data to at least 1 row."
    )

    x, y = dataset[0]
    assert x is not None, f"FAIL [{dataset_name}] Zenodo test: dataset[0] returned x=None."
    assert y is not None, f"FAIL [{dataset_name}] Zenodo test: dataset[0] returned y=None."
