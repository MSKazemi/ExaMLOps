"""
Shared utilities for CI validation scripts.
Discovery of model and dataset classes from Python files.
"""

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, Dict, Type


def import_module_from_file(file_path: Path) -> Any:
    """
    Dynamically import a Python module from a file path.

    Args:
        file_path: Path to the Python file

    Returns:
        Imported module object
    """
    # Use full path for unique module name to avoid collisions
    module_name = f"_{file_path.stem}_{hash(str(file_path)) % 10**8}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def retrieve_instances_from_file(file_path: Path, class_type: Type) -> Dict[str, Type]:
    """
    Find all classes in a Python file that inherit from class_type.

    Returns:
        Dict mapping class names to class objects (not instances).
    """
    instances: Dict[str, Type] = {}
    module = import_module_from_file(file_path)

    for name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, class_type) and obj.__module__ == module.__name__:
            instances[name] = obj

    return instances
