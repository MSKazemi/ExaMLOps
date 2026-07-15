"""
Fixtures and helpers for integration tests.

Uses sample_data from tests/fixtures/sample_data/ (create with `make sample-data`).

No model or dataset names are hardcoded here. All helpers are generic and work
with any model that implements get_train_config().
"""

from pathlib import Path
from typing import Optional, Tuple

import pytest


def _integration_data_dir() -> Path:
    """Path to tests/fixtures/sample_data."""
    root = Path(__file__).resolve().parent.parent
    return root / "fixtures" / "sample_data"


@pytest.fixture(scope="session")
def integration_data_dir():
    """Path to tests/fixtures/sample_data. Run `make sample-data` if missing."""
    d = _integration_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def temp_model_dir(tmp_path):
    """Temporary directory for saving models during tests."""
    return tmp_path


# ---------------------------------------------------------------------------
# Generic helpers — used by test_model_train.py and test_model_save_load.py
# ---------------------------------------------------------------------------


def fixture_path_for(dataset_cls, data_dir: Path) -> Path:
    """
    Return the expected fixture path for a dataset class.

    Naming convention mirrors create_sample_data.py:
      FDataDataset  → sample_fdata.parquet
      PM100Dataset  → sample_pm100.parquet
    """
    name = dataset_cls.__name__.lower().replace("dataset", "")
    return data_dir / f"sample_{name}.parquet"


def load_first_fixture_entry(model_name: str, model_cls, data_dir: Path):
    """
    Find the first dataset entry in get_train_config() that has a fixture file,
    load the dataset from that fixture, and return (model_instance, dataset, loader).

    Calls pytest.skip() if no fixture is available for any entry.

    This helper is the offline counterpart to test_model_pipeline.py which uses
    is_dummy=True. These fixture-based tests work without network access as long
    as `make sample-data` has been run.
    """
    pytest.importorskip("sklearn")
    pytest.importorskip("torch")

    from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader

    config = model_cls.get_train_config()
    tried = []

    for entry_name, (model_instance, dataset_cls, (ds_params, dl_params)) in config.items():
        fixture = fixture_path_for(dataset_cls, data_dir)
        tried.append(f"{entry_name} → {fixture.name}")

        if not fixture.exists():
            continue

        # Override data_path to point at the local fixture and clear filters.
        # Date/range filters from get_train_config() are designed for the full
        # Zenodo dataset — applying them to a small fixture sample would likely
        # return 0 rows. All other params (features, transforms, etc.) are kept.
        local_params = ds_params.model_copy(update={"data_path": fixture, "filters": None})

        try:
            dataset = dataset_cls.from_config(local_params)
            dataset.load_data()
        except Exception as e:
            pytest.skip(
                f"{model_name}/{entry_name}: failed to load fixture {fixture.name}: {e}"
            )

        if len(dataset) == 0:
            pytest.skip(
                f"{model_name}/{entry_name}: fixture {fixture.name} loaded 0 rows."
            )

        loader = SeanergysDataloader.from_config(
            data_loader_config=dl_params, dataset=dataset
        )
        return model_instance, dataset, loader

    pytest.skip(
        f"{model_name}: no fixture file found for any dataset in get_train_config().\n"
        f"  Entries checked: {', '.join(tried)}\n"
        f"  Run `make sample-data` to create fixture files."
    )
