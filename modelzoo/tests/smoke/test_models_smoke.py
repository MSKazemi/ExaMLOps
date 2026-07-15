"""
Smoke tests: imports, discovery, model instantiation.

TODO: When you add real dataset sources / max_samples support:
  - Add train(1 step) smoke with synthetic data
  - See docs/TESTING_AND_CI_PLAN.md Phase 2
"""

import pytest


def test_logger_imports():
    """SeanergysLogger can be imported and instantiated."""
    from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger

    logger = SeanergysLogger()
    assert logger is not None


def test_ci_utils_imports():
    """ci.utils discovery helpers can be imported."""
    from ci.utils import import_module_from_file, retrieve_instances_from_file

    assert callable(import_module_from_file)
    assert callable(retrieve_instances_from_file)


def test_model_discovery(project_root):
    """
    Discover all SeanergysModel classes. Requires full deps (torch, sklearn, etc).
    Skipped if deps missing (use `poetry install --with ci` for full run).
    """
    pytest.importorskip("torch", reason="Model discovery requires torch")
    pytest.importorskip("sklearn", reason="Model discovery requires sklearn")
    from ci.utils import retrieve_instances_from_file
    from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel

    model_folder = project_root / "seanergys_modelzoo" / "models"
    model_classes = {}

    for py_file in model_folder.rglob("*.py"):
        try:
            objs = retrieve_instances_from_file(py_file, SeanergysModel)
            model_classes.update(objs)
        except Exception:
            continue

    assert len(model_classes) >= 1, "Should find at least one SeanergysModel subclass"


def test_model_instantiation(project_root):
    """
    Each SeanergysModel subclass instantiates with minimal config. No training.
    Validates model discovery and basic construction.
    """
    pytest.importorskip("torch", reason="Model instantiation requires torch")
    pytest.importorskip("sklearn", reason="Model instantiation requires sklearn")
    from ci.utils import retrieve_instances_from_file
    from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel

    model_folder = project_root / "seanergys_modelzoo" / "models"
    model_classes = {}

    for py_file in model_folder.rglob("*.py"):
        try:
            objs = retrieve_instances_from_file(py_file, SeanergysModel)
            model_classes.update(objs)
        except Exception:
            continue

    # Minimal hyperparams that work for RF, XGBoost, etc.
    minimal_hp = {"n_estimators": 2, "n_jobs": 1}

    for name, model_cls in model_classes.items():
        # Skip base/abstract classes by name, and any class that still has
        # unimplemented abstract methods (e.g. SeanergysHuggingFaceModel in
        # models/common/). The session conftest only scans models/tasks/ for the
        # same reason; this rglob over all of models/ must guard explicitly.
        if model_cls.__name__ in ("SeanergysModel", "SeanergysSklearnModel"):
            continue
        if getattr(model_cls, "__abstractmethods__", frozenset()):
            continue
        try:
            model = model_cls(model_hyperparameters=minimal_hp)
            assert model is not None
            assert hasattr(model, "model_name")
        except Exception as e:
            pytest.fail(f"{name} failed to instantiate: {e}")
