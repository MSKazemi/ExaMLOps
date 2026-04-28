"""
Unit tests for ci.utils (discovery helpers).
"""

from pathlib import Path

import pytest


def test_import_module_from_file(tmp_path):
    """import_module_from_file loads a Python file as a module."""
    from ci.utils import import_module_from_file

    py_file = tmp_path / "foo.py"
    py_file.write_text("x = 42\n")

    module = import_module_from_file(py_file)
    assert module.x == 42


def test_import_module_from_file_missing_raises():
    """import_module_from_file raises OSError/FileNotFoundError for non-existent file."""
    from ci.utils import import_module_from_file

    with pytest.raises((OSError, FileNotFoundError)):
        import_module_from_file(Path("/nonexistent/file.py"))


def test_retrieve_instances_from_file_finds_classes(tmp_path):
    """retrieve_instances_from_file finds classes inheriting from base."""
    from ci.utils import retrieve_instances_from_file

    py_file = tmp_path / "myclasses.py"
    py_file.write_text(
        """
class Base:
    pass

class Foo(Base):
    pass

class Bar(Base):
    pass
"""
    )

    # Pass object so any class in the module is found (all inherit from object)
    result = retrieve_instances_from_file(py_file, object)
    assert "Base" in result
    assert "Foo" in result
    assert "Bar" in result
    assert len(result) == 3


def test_retrieve_instances_from_file_empty(tmp_path):
    """retrieve_instances_from_file returns empty dict when no class matches."""
    from ci.utils import retrieve_instances_from_file

    py_file = tmp_path / "no_classes.py"
    py_file.write_text("x = 1\ny = 2\n")

    # Use a base class that no local class inherits from
    class Unrelated:
        pass

    result = retrieve_instances_from_file(py_file, Unrelated)
    assert result == {}
