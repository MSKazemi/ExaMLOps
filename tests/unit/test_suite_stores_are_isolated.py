# tests/unit/test_suite_stores_are_isolated.py
"""No unit test reads the platform's real SQLite stores (BL-062).

Found chasing a flake ab saw once under `-n 6` in `test_sqlite_tier_skips_platform_under_postgres`.
With `AGENT_MEMORY_DB` / `AGENT_DB` / `AGENT_MEMORY_REVIEW_DB` / `MLFLOW_SQLITE_DB` unset, those
stores default to paths relative to the working directory, which is the repository root when the
suite runs and where the live dev stack keeps them. `exa backup`'s sqlite tier then copied the
developer's real agent memory and MLflow databases into the test's backup, mid-write. The fix is
`tests/conftest.py::_isolate_sqlite_stores`; this holds it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

from tests.conftest import _CWD_RELATIVE_STORES  # noqa: E402


def test_every_cwd_relative_store_is_this_tests_own(tmp_path):
    from examlops.backup import sqlite_tier

    declared = {s["env"] for s in sqlite_tier._SQLITE_DBS if s["default"].startswith("./")}
    assert declared <= set(_CWD_RELATIVE_STORES), (
        f"a store with a CWD-relative default is not isolated: {sorted(declared - set(_CWD_RELATIVE_STORES))}"
    )
    for var in _CWD_RELATIVE_STORES:
        path = Path(os.environ[var])
        assert path.is_absolute() and not path.resolve().is_relative_to(ROOT), var


def test_a_backup_run_by_the_suite_copies_no_real_store(tmp_path):
    from examlops.backup import sqlite_tier
    from examlops.backup._manifest import OK

    res = sqlite_tier.backup_sqlite_tier(tmp_path)

    copied = {i["name"] for i in res.items if i["status"] == OK}
    assert not copied & {"skipper_memory", "agent_memory", "skipper_review", "mlflow"}, copied


def test_the_checkout_stores_cannot_be_opened():
    """The conftest audit hook refuses a connection to the checkout's own stores and records it,
    so a test fails even when the code under test swallows the error.

    Asked of the hook directly, because it must hold on **either** datastore engine. Four of the
    five stores it names are SQLite whatever `EXAMLOPS_DB_BACKEND` says — the agent's memory, its
    review store and MLflow's — so this is exactly as load-bearing on a Postgres install as on a
    SQLite one, and the seam below cannot reach it there.
    """
    import sqlite3

    import pytest

    from tests import conftest

    for store in sorted(conftest._CHECKOUT_STORES):
        with pytest.raises(PermissionError, match="checkout's own store"):
            sqlite3.connect(store)
    assert sorted(set(conftest._CHECKOUT_STORE_OPENS)) == sorted(conftest._CHECKOUT_STORES), (
        "every refusal is recorded, so the teardown check fails the test that caused it"
    )
    conftest._CHECKOUT_STORE_OPENS.clear()  # opened on purpose here


def test_a_platform_db_left_in_the_checkout_is_refused_through_get_db(monkeypatch):
    """The same guard through the real seam: a `PLATFORM_DB` still pointing at the checkout's own
    file, which is the accident it was written for.

    SQLite only, and not as a concession — on Postgres `PLATFORM_DB` names nothing the platform
    opens, so the accident this describes cannot happen there. Skipping says that; passing
    vacuously (the hook never fires, `get_db` succeeds against the shared schema) would not.
    """
    import pytest

    from examlops.platform_db import get_db
    from examlops.storage.testing import postgres_backend
    from tests import conftest

    if postgres_backend():
        pytest.skip("PLATFORM_DB is not the datastore on Postgres; the hook is covered above")

    monkeypatch.setenv("PLATFORM_DB", str(conftest.REPO_ROOT / "platform.db"))
    with pytest.raises(PermissionError, match="checkout's own store"):
        with get_db() as conn:
            conn.execute("SELECT 1")
    assert conftest._CHECKOUT_STORE_OPENS, "recorded, so the teardown check fails the test"
    conftest._CHECKOUT_STORE_OPENS.clear()  # opened on purpose here
