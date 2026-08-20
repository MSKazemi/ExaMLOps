"""Shared pytest fixtures."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolate_postgres_state():
    """Give every test an empty platform database when running on Postgres.

    The mechanism lives in :mod:`examlops.storage.testing` because the dashboard's suite — a
    separate app with its own connection adapter — needs exactly the same thing, and a copy in
    two conftests would drift. No-op on SQLite, where each test gets its own ``tmp_path`` file.
    """
    from examlops.storage.testing import postgres_isolation

    yield from postgres_isolation()
