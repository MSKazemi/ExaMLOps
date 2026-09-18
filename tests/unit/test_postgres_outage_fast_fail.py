"""A datastore outage must cost milliseconds, not minutes (plan P5, chaos drill).

The chaos drill (`tests/integration/test_datastore_outage_drill_live.py`) killed Postgres under the
control plane and found `/readyz` hanging past 40 seconds: every request that touched the store held
a worker for the pool's full 30 s budget, and a connection whose server had vanished waited on
kernel TCP retransmission, because nothing bounded it. A service whose workers all wait on a dead
store answers nothing at all — not even the probe that would take it out of rotation.

These tests pin the four things that bound it, without needing a server:

- every connection carries libpq's timeouts (`connect_timeout`, keepalives, `tcp_user_timeout`),
  unless the DSN names its own;
- a checkout waits `EXAMLOPS_POSTGRES_POOL_TIMEOUT` (2 s), not psycopg_pool's 30;
- the first failure is remembered, so the calls that follow fail at once — and the memory expires,
  so recovery needs no restart;
- the failed pool is discarded, so the next attempt reconnects instead of sitting out the pool's
  exponential backoff (16 s in the drill, 0.03 s after this).
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("psycopg")

from examlops.storage import pg  # noqa: E402

DSN = "postgresql://examlops:secret@db.example:5432/examlops"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pg._UNREACHABLE.clear()
    with pg._POOL_LOCK:
        pg._POOLS.clear()
    for var in (
        "EXAMLOPS_POSTGRES_POOL_TIMEOUT",
        "EXAMLOPS_POSTGRES_CONNECT_TIMEOUT",
        "EXAMLOPS_POSTGRES_TCP_TIMEOUT_MS",
        "EXAMLOPS_POSTGRES_UNREACHABLE_TTL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    pg._UNREACHABLE.clear()


# ── what bounds a single connection ───────────────────────────────────────────


def test_every_connection_is_bounded_at_the_tcp_level():
    kwargs = pg._connection_kwargs(DSN)
    assert kwargs["connect_timeout"] >= 1  # libpq wants whole seconds
    assert kwargs["keepalives"] == 1 and kwargs["keepalives_idle"] > 0
    # The one that bounds a query whose server vanished: keepalives alone only help an idle socket.
    assert kwargs["tcp_user_timeout"] == 10000


def test_a_dsn_that_names_a_timeout_keeps_its_own():
    kwargs = pg._connection_kwargs(DSN + "?connect_timeout=17&tcp_user_timeout=1234")
    assert "connect_timeout" not in kwargs and "tcp_user_timeout" not in kwargs
    assert kwargs["keepalives"] == 1  # the rest still apply


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 10000), ("2500", 2500), ("0", 1000), ("nonsense", 10000)],
)
def test_the_tcp_timeout_is_configurable_with_a_floor(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("EXAMLOPS_POSTGRES_TCP_TIMEOUT_MS", value)
    assert pg._tcp_user_timeout_ms() == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 2.0), ("0.75", 0.75), ("0.1", 0.5), ("", 2.0), ("nonsense", 2.0)],
)
def test_a_checkout_waits_two_seconds_by_default(monkeypatch, value, expected):
    """Long enough for a healthy pool, short enough to fit inside a readiness probe."""
    if value is not None:
        monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_TIMEOUT", value)
    assert pg._pool_timeout() == expected


# ── the outage path ───────────────────────────────────────────────────────────


class _DeadPool:
    """A pool whose server is gone: every checkout fails after the pool's budget."""

    def __init__(self) -> None:
        self.checkouts = 0
        self.closed = False

    def getconn(self):
        self.checkouts += 1
        raise TimeoutError("couldn't get a connection after 2.00 sec")

    def close(self, timeout: float = 5.0) -> None:
        self.closed = True


def _serve(monkeypatch, pool) -> list[int]:
    """Hand `connect()` this pool, counting how often it asks for one."""
    asked: list[int] = []

    def _get_pool(dsn, schema):
        asked.append(1)
        with pg._POOL_LOCK:
            pg._POOLS[(dsn, schema)] = pool
        return pool

    monkeypatch.setattr(pg, "_get_pool", _get_pool)
    return asked


def test_a_failed_checkout_is_a_clear_datastore_error(monkeypatch):
    import psycopg

    _serve(monkeypatch, _DeadPool())
    with pytest.raises(psycopg.OperationalError) as caught:
        pg.connect(DSN)
    message = str(caught.value)
    assert "datastore unavailable at db.example:5432" in message
    assert "2s" in message and "couldn't get a connection" in message  # the budget and the cause


def test_the_calls_after_it_fail_at_once_and_then_try_again(monkeypatch):
    import psycopg

    pool = _DeadPool()
    asked = _serve(monkeypatch, pool)
    with pytest.raises(psycopg.OperationalError):
        pg.connect(DSN)
    assert pool.checkouts == 1 and len(asked) == 1

    began = time.monotonic()
    for _ in range(50):
        with pytest.raises(psycopg.OperationalError):
            pg.connect(DSN)
    # Fifty calls during the outage, none of which touched the datastore or waited for it.
    assert time.monotonic() - began < 0.5
    assert pool.checkouts == 1 and len(asked) == 1

    # The verdict expires, so recovery needs no restart: the next call tries the server again.
    pg._UNREACHABLE.clear()
    with pytest.raises(psycopg.OperationalError):
        pg.connect(DSN)
    assert len(asked) == 2


def test_the_verdict_expires_on_its_own(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_POSTGRES_UNREACHABLE_TTL", "0.2")
    pg._mark_unreachable(DSN, "gone")
    assert pg._unreachable_now(DSN) == "gone"
    time.sleep(0.3)
    assert pg._unreachable_now(DSN) is None
    assert not pg._UNREACHABLE  # and the expired entry is not left behind


def test_a_failed_pool_is_discarded_so_the_next_attempt_reconnects(monkeypatch):
    import psycopg

    pool = _DeadPool()
    _serve(monkeypatch, pool)
    with pytest.raises(psycopg.OperationalError):
        pg.connect(DSN)
    with pg._POOL_LOCK:
        assert (DSN, None) not in pg._POOLS, "the dead pool is still cached"
    assert _wait(lambda: pool.closed), "the dead pool was never closed"


def _wait(fn, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize(
    ("dsn", "endpoint"),
    [
        (DSN, ("db.example", 5432)),
        ("postgresql://u@db.example/examlops", ("db.example", 5432)),
        ("postgresql://u@/examlops?host=/var/run/postgresql", None),  # unix socket
        ("postgresql://u@a.example,b.example:5432/examlops", None),  # multi-host failover
    ],
)
def test_only_a_single_host_tcp_datastore_is_remembered(dsn, endpoint):
    """A multi-host DSN is libpq's failover to make; remembering "unreachable" would defeat it."""
    assert pg._dsn_endpoint(dsn) == endpoint
    pg._mark_unreachable(dsn, "gone")
    assert (pg._unreachable_now(dsn) == "gone") is (endpoint is not None)


class _RefusingPool(_DeadPool):
    """A server that answered and said no: a wrong password, a missing database."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def getconn(self):
        import psycopg

        self.checkouts += 1
        raise psycopg.OperationalError(self._message)


@pytest.mark.parametrize(
    "message",
    [
        'password authentication failed for user "examlops"',
        'database "nope" does not exist',
        'permission denied for schema "exa_test"',
    ],
)
def test_a_server_that_refuses_is_not_remembered_as_unreachable(monkeypatch, message):
    """Otherwise one wrong password would fail every other caller of that server for the TTL —
    and hide the real error behind "datastore unavailable"."""
    import psycopg

    pool = _RefusingPool(message)
    asked = _serve(monkeypatch, pool)
    for _ in range(3):
        with pytest.raises(psycopg.OperationalError) as caught:
            pg.connect(DSN)
        assert message in str(caught.value)
        assert "datastore unavailable" not in str(caught.value)
    assert pool.checkouts == 3 and len(asked) == 3  # every call still reached the server
    assert not pg._UNREACHABLE
    with pg._POOL_LOCK:
        assert (DSN, None) in pg._POOLS  # and its pool is intact


@pytest.mark.parametrize(
    ("text", "unreachable"),
    [
        ("connection refused", True),
        ("timeout expired", True),
        ("couldn't get a connection after 2.00 sec", True),
        ("server closed the connection unexpectedly", True),
        ('password authentication failed for user "x"', False),
        ('relation "audit_events" does not exist', False),
        ("duplicate key value violates unique constraint", False),
    ],
)
def test_unreachability_is_told_apart_from_a_refusal(text, unreachable):
    import psycopg

    assert pg._is_unreachable_error(psycopg.OperationalError(text)) is unreachable
