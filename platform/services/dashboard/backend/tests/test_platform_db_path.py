"""One datastore location for the whole dashboard backend, the same one the core package uses.

Forty-five backend modules each resolved the datastore as ``os.getenv("PLATFORM_DB",
"/repo/platform.db")``. The shared ``examlops`` code the dashboard also calls resolves it through
the ADR 0128 data root, so a process with ``EXAMLOPS_DATA_DIR`` set and ``PLATFORM_DB`` unset
read two different files — the consoles and the CLI Console disagreeing with no error anywhere.
``dbconn.platform_db_path()`` is now the only resolver, and these tests hold it to the core's.
"""

from __future__ import annotations

from pathlib import Path

import dbconn
import pytest

from examlops import platform_db as core

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("PLATFORM_DB", raising=False)
    monkeypatch.delenv("EXAMLOPS_DATA_DIR", raising=False)
    return monkeypatch


def test_platform_db_wins_everywhere(clean_env, tmp_path):
    explicit = str(tmp_path / "explicit.db")
    clean_env.setenv("PLATFORM_DB", explicit)
    clean_env.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "root"))
    assert dbconn.platform_db_path() == explicit == core._db_path()


def test_the_data_root_resolves_like_the_core(clean_env, tmp_path):
    clean_env.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    assert dbconn.platform_db_path() == core._db_path() == str(tmp_path / "platform.db")


def test_an_empty_platform_db_counts_as_unset(clean_env, tmp_path):
    clean_env.setenv("PLATFORM_DB", "")
    clean_env.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    assert dbconn.platform_db_path() == str(tmp_path / "platform.db")


def test_without_either_the_legacy_compose_location_stands(clean_env):
    assert dbconn.platform_db_path() == "/repo/platform.db"


def test_no_other_backend_module_names_the_path():
    """A second hard-coded default is how the two files came apart in the first place."""
    offenders = [
        p.relative_to(BACKEND).as_posix()
        for p in BACKEND.rglob("*.py")
        if "tests" not in p.parts
        and p.name not in {"dbconn.py", "settings.py"}
        and "/repo/platform.db" in p.read_text()
    ]
    assert not offenders, offenders
