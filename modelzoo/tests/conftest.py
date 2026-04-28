"""
Pytest configuration and shared fixtures.

Auto-discovery at collection time:
  MODEL_CLASSES   — all concrete SeanergysModel subclasses in models/tasks/
  DATASET_CLASSES — all concrete SeanergysDataset subclasses in datasets/

Both dicts are used to parametrize integration and smoke tests.
Adding a new model or dataset to the repo automatically adds it to the test suite.
"""

import os
from pathlib import Path
from typing import Dict, Type

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    """Ensure project root is on Python path."""
    root = str(_PROJECT_ROOT)
    if root not in os.environ.get("PYTHONPATH", ""):
        os.environ.setdefault("PYTHONPATH", root)


# ---------------------------------------------------------------------------
# Auto-discovery helpers
# ---------------------------------------------------------------------------

def _collect_model_classes() -> Dict[str, Type]:
    """
    Discover all concrete SeanergysModel subclasses in models/tasks/.
    Only scans tasks/ so base/abstract classes are never included.
    Returns empty dict if deps (torch, sklearn) are not installed.
    """
    try:
        from ci.utils import retrieve_instances_from_file
        from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel
    except ImportError:
        return {}

    classes: Dict[str, Type] = {}
    for py_file in (_PROJECT_ROOT / "seanergys_modelzoo" / "models" / "tasks").rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            classes.update(retrieve_instances_from_file(py_file, SeanergysModel))
        except Exception:
            continue
    return classes


def _collect_dataset_classes() -> Dict[str, Type]:
    """
    Discover all concrete SeanergysDataset subclasses in datasets/.
    Excludes abstract base classes (SeanergysDataset, SeanergysParquetDataset).
    Returns empty dict if deps (torch) are not installed.
    """
    try:
        from ci.utils import retrieve_instances_from_file
        from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset
    except ImportError:
        return {}

    _BASE_CLASSES = {"SeanergysDataset", "SeanergysParquetDataset"}
    classes: Dict[str, Type] = {}

    for py_file in (_PROJECT_ROOT / "seanergys_modelzoo" / "datasets").rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            objs = retrieve_instances_from_file(py_file, SeanergysDataset)
            classes.update({k: v for k, v in objs.items() if v.__name__ not in _BASE_CLASSES})
        except Exception:
            continue
    return classes


# Collected once at session start. Used to parametrize tests.
# Any model/dataset added to the repo is automatically included.
MODEL_CLASSES = _collect_model_classes()
DATASET_CLASSES = _collect_dataset_classes()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def project_root() -> Path:
    return _PROJECT_ROOT


@pytest.fixture(scope="session")
def ci_max_samples() -> int:
    """Max samples for CI runs. Override via env CI_MAX_SAMPLES."""
    return int(os.environ.get("CI_MAX_SAMPLES", "50"))


@pytest.fixture(scope="session")
def sample_data_dir(project_root) -> Path:
    """Path to tests/fixtures/sample_data/. Create with `make sample-data`."""
    d = project_root / "tests" / "fixtures" / "sample_data"
    d.mkdir(parents=True, exist_ok=True)
    return d
