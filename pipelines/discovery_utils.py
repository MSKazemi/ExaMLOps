"""Class-discovery utilities for the pipeline engine.

Generic, dependency-free helpers to dynamically import a Python file and find the
concrete classes it defines that subclass a given base. Relocated here from
``modelzoo/ci/utils.py`` so the platform pipeline engine no longer reaches into the
model library's CI helpers — keeping the platform ⟂ use-case boundary clean (ADR 0094)
and making the engine importable in a runtime that ships only ``pipelines`` (e.g. the
serving/pipeline container, which does not vendor ``modelzoo/ci``).
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any


def import_module_from_file(file_path: Path) -> Any:
    """Dynamically import a Python module from a file path."""
    # Full-path-derived unique module name to avoid collisions across packs.
    module_name = f"_{file_path.stem}_{hash(str(file_path)) % 10**8}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def retrieve_instances_from_file(file_path: Path, class_type: type) -> dict[str, type]:
    """Return the concrete classes defined in ``file_path`` that subclass ``class_type``.

    Keyed by class name; only classes whose ``__module__`` is the loaded file are
    returned (so imported base classes are not picked up).
    """
    instances: dict[str, type] = {}
    module = import_module_from_file(file_path)
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, class_type) and obj.__module__ == module.__name__:
            instances[name] = obj
    return instances
