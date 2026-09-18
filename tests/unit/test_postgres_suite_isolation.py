# tests/unit/test_postgres_suite_isolation.py
"""The Postgres suite's own isolation, which nothing else can check for it.

`examlops.storage.testing` is the reason the unit suite can run on both engines: on SQLite each
test gets a private `PLATFORM_DB` file, and on Postgres these helpers stand in for that. They are
test-support code, which is exactly why they need tests — when this module is wrong the failure
does not look like a broken helper, it looks like a broken platform, and the last two engine-parity
sweeps both lost time to that shape:

- a helper that quietly gives two tests the same schema turns into other tests' failures, in
  whichever order pytest happened to pick;
- a helper that quietly gives a test a *bootstrapped* schema turns a "degrade when the table is
  absent" test into a vacuous pass, which is worse than a failure because nobody looks at it.

So the contract is asserted directly, and without a server: every function here decides what to do
from the environment, so the environment is all a test needs to drive it.
"""

from __future__ import annotations

import pytest

from examlops.storage import testing as st


@pytest.fixture(autouse=True)
def _no_backend(monkeypatch):
    """Each test states the environment it is about; none of them inherits the suite's."""
    monkeypatch.delenv("EXAMLOPS_DB_BACKEND", raising=False)
    monkeypatch.delenv("EXAMLOPS_POSTGRES_SCHEMA", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)


def _postgres(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")


def test_a_worker_gets_its_own_schema(monkeypatch):
    _postgres(monkeypatch)
    monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", "exa_test")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")

    assert st.scope_schema_to_this_worker() == "exa_test_gw3"
    import os

    assert os.environ["EXAMLOPS_POSTGRES_SCHEMA"] == "exa_test_gw3", "the change must be in force"


def test_two_workers_never_get_the_same_schema(monkeypatch):
    """The property that matters: without it, workers truncate each other's rows mid-test."""
    _postgres(monkeypatch)
    monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", "exa_test")
    seen = set()
    for worker in ("gw0", "gw1", "gw2", "gw7"):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", "exa_test")  # as each worker starts
        seen.add(st.scope_schema_to_this_worker())
    assert len(seen) == 4, seen


def test_a_serial_run_keeps_the_schema_it_was_given(monkeypatch):
    """`make test-postgres` names `exa_test`, and a serial run must still be that schema —
    the backup tier's tests read it back by name."""
    _postgres(monkeypatch)
    monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", "exa_test")

    assert st.scope_schema_to_this_worker() is None
    import os

    assert os.environ["EXAMLOPS_POSTGRES_SCHEMA"] == "exa_test"


def test_sqlite_is_untouched(monkeypatch):
    """The default engine must not acquire a Postgres-shaped variable it never reads."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")

    assert st.scope_schema_to_this_worker() is None
    import os

    assert "EXAMLOPS_POSTGRES_SCHEMA" not in os.environ


def test_an_unnamed_schema_becomes_a_named_one_per_worker(monkeypatch):
    """With no schema configured Postgres would use `public` — shared by every worker, and by
    anything else on that server. A parallel run must not land there."""
    _postgres(monkeypatch)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")

    assert st.scope_schema_to_this_worker() == "public_gw2"


def test_the_isolation_fixture_is_a_no_op_on_sqlite():
    """It must not import platform_db or touch a server when the engine is SQLite: the whole
    SQLite suite runs through this generator on every test."""
    gen = st.postgres_isolation()
    next(gen)  # would connect on Postgres; here it must simply yield
    with pytest.raises(StopIteration):
        next(gen)


def test_a_worker_gets_a_worker_sized_pool(monkeypatch):
    """The service default (max 10) times eight workers times the sibling schemas took a `-n auto`
    run past Postgres' 100-connection limit: 149 errors that said `too many clients already` and
    nothing about the platform."""
    import os

    _postgres(monkeypatch)
    monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", "exa_test")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_POOL_MAX", raising=False)
    monkeypatch.delenv("EXAMLOPS_POSTGRES_POOL_MIN", raising=False)

    st.scope_schema_to_this_worker()
    pool_max = int(os.environ["EXAMLOPS_POSTGRES_POOL_MAX"])
    assert pool_max <= 2, "a worker runs one test at a time; `-n auto` is 24 of them here"
    assert int(os.environ["EXAMLOPS_POSTGRES_POOL_MIN"]) == 1
    # The arithmetic that decides it: workers, their sibling schemas, and the subprocesses the CLI
    # tests spawn, against a stock server's 100.
    assert pool_max * 24 + 24 < 100


def test_an_explicit_pool_size_is_kept(monkeypatch):
    """A suite that means to exercise a service-shaped pool must be able to say so."""
    import os

    _postgres(monkeypatch)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", "9")

    st.scope_schema_to_this_worker()
    assert os.environ["EXAMLOPS_POSTGRES_POOL_MAX"] == "9"
