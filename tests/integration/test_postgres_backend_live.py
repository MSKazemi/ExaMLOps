"""The platform's real helpers, run against a live Postgres (enterprise-readiness item 0.1).

This is the test that turns "the Postgres backend exists" into "the Postgres backend works": it
runs the **unmodified** ``platform_db``/``examlops.data`` helpers with
``EXAMLOPS_DB_BACKEND=postgres`` and asserts the same results the SQLite path gives — including the
two properties a datastore port most easily loses: the audit **hash chain** and the **append-only**
tamper-evidence trigger on ``audit_events``.

Opt-in, because it needs a server and it **destroys the target schema**::

    docker run -d --name examlops-pgtest -e POSTGRES_PASSWORD=examlops -e POSTGRES_USER=examlops \
        -e POSTGRES_DB=examlops -p 15433:5432 postgres:16-alpine
    EXAMLOPS_POSTGRES_TEST_DSN=postgresql://examlops:examlops@localhost:15433/examlops \
        .venv/bin/pytest tests/integration/test_postgres_backend_live.py -v

The variable is deliberately **not** ``EXAMLOPS_POSTGRES_DSN``: pointing a suite that runs
``DROP SCHEMA public CASCADE`` at whatever DSN the environment happens to carry is how a test
eats a real database. A separate opt-in name cannot be set by accident.
"""

from __future__ import annotations

import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "cli" / "src"))

DSN = os.getenv("EXAMLOPS_POSTGRES_TEST_DSN", "")

pytestmark = pytest.mark.skipif(not DSN, reason="set EXAMLOPS_POSTGRES_TEST_DSN to run")


@pytest.fixture(autouse=True)
def pg_schema(monkeypatch):
    """A pristine schema per test, so ordering can never make one test depend on another."""
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", DSN)
    from examlops import platform_db as pdb

    pdb.init_db(force=True)
    yield pdb


def _table_count() -> int:
    import psycopg

    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchone()
    return int(row[0])


def test_full_schema_creates_on_postgres(pg_schema):
    """The 127-table SQLite DDL — AUTOINCREMENT, DATETIME defaults, triggers — ports whole."""
    assert _table_count() >= 120


def test_audit_hash_chain_verifies(pg_schema):
    for i in range(3):
        pg_schema.write_audit_event("cli", "tester", f"action_{i}", "JPCP", {"i": i})
    chain = pg_schema.verify_audit_chain()
    assert chain["ok"] is True
    assert chain["count"] == 3
    assert chain["head_hash"] != "GENESIS"
    assert len(pg_schema.export_audit_events()) == 3


@pytest.mark.parametrize("stmt", ["UPDATE audit_events SET actor='x'", "DELETE FROM audit_events"])
def test_audit_log_is_append_only(pg_schema, stmt):
    """D4 tamper-evidence is a security control, so it is translated, not dropped."""
    pg_schema.write_audit_event("cli", "tester", "action", "JPCP", None)
    with pytest.raises(Exception, match="append-only"):
        with pg_schema.get_db() as conn:
            conn.execute(stmt)


def test_upsert_replaces_rather_than_duplicates(pg_schema):
    """`INSERT OR REPLACE` must land on the table's real key, not append a second row."""
    pg_schema.set_traffic_rules("JPCP", {"production": 90, "canary": 10})
    pg_schema.set_traffic_rules("JPCP", {"production": 50, "canary": 50})
    assert pg_schema.get_traffic_rules("JPCP") == {"production": 50, "canary": 50}


def test_timestamps_are_sqlite_shaped_strings(pg_schema):
    """Helpers slice and compare ts as text; a native timestamp would change 252 return types."""
    pg_schema.write_drift_snapshot("JPCP", "Production", 1.5, "job-1")
    with pg_schema.get_db() as conn:
        ts = conn.execute("SELECT ts FROM drift_snapshots").fetchone()["ts"]
    assert isinstance(ts, str)
    assert len(ts) == 19 and ts[4] == "-" and ts[13] == ":"


def test_row_is_addressable_by_name_and_index(pg_schema):
    with pg_schema.get_db() as conn:
        row = conn.execute("SELECT 1 AS a, 2 AS b").fetchone()
    assert row["a"] == 1 and row[1] == 2


def test_lastrowid_survives_the_port(pg_schema):
    """Postgres has no rowid; the wrapper reads the serial back via RETURNING."""
    with pg_schema.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO audit_events (source, actor, action, target) VALUES (?,?,?,?)",
            ("cli", "tester", "a", "t"),
        )
    assert cur.lastrowid == 1


def test_cooldown_claim_uses_translated_date_math(pg_schema):
    """`claim_drift_trigger` is the julianday() site — and the autopilot's TOCTOU guard."""
    assert pg_schema.claim_drift_trigger("JPCP", 60) is False  # no config row yet


def test_project_anatomy_round_trips(pg_schema):
    pg_schema.create_project("research", cpu_limit=4, memory_limit_gb=8, storage_gb=100)
    full = pg_schema.get_project_full("research")
    assert full["name"] == "research"
    assert full["cpu_limit"] == 4


def test_control_plane_state_round_trips_on_postgres(pg_schema, monkeypatch):
    """The separate approval/ModelZoo store must use the same production backend seam."""
    control_plane_dir = (
        Path(__file__).resolve().parents[2] / "platform" / "services" / "control_plane"
    )
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "integration-test-token")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.syspath_prepend(str(control_plane_dir))
    spec = spec_from_file_location("control_plane_postgres_live", control_plane_dir / "app.py")
    assert spec and spec.loader
    control_plane = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, control_plane)
    spec.loader.exec_module(control_plane)

    conn = control_plane._get_db()
    cursor = conn.execute(
        "INSERT INTO pending_approvals (id, model_id, status, requested_at) VALUES (?, ?, ?, ?)",
        ("cp-pg-1", "JPCP", "pending", "2026-08-25T00:00:00"),
    )
    assert cursor.rowcount == 1
    event = conn.execute(
        "INSERT INTO modelzoo_events "
        "(commit_sha, branch, pushed_by, timestamp, source) VALUES (?, ?, ?, ?, ?)",
        ("abc123", "main", "integration", "2026-08-25T00:00:00", "test"),
    )
    event_id = event.lastrowid
    conn.commit()
    conn.close()

    conn = control_plane._get_db()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE status=?", ("pending",)
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT commit_sha FROM modelzoo_events WHERE id=?", (event_id,)).fetchone()[0]
        == "abc123"
    )
    conn.close()


# ── connection pooling ────────────────────────────────────────────────────────


def test_pooling_reuses_physical_connections(pg_schema, monkeypatch):
    """The point of the pool: ten checkouts must not cost ten backend processes.

    Backend pids are the only honest evidence of reuse — a timing assertion would be flaky on a
    loaded CI box. The pool opens its minimum size in the background, so the assertion is on the
    *number of distinct* backends, not on a single identity.
    """
    from examlops.storage import pg as pgmod

    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL", "1")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", "4")
    pgmod.close_pools()
    try:
        pids = set()
        for _ in range(10):
            conn = pgmod.connect(DSN)
            pids.add(conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"])
            conn.close()
        assert len(pids) <= 4  # bounded by the pool, not by the number of calls
    finally:
        pgmod.close_pools()


def test_pooling_off_opens_a_fresh_connection_every_time(pg_schema, monkeypatch):
    """`EXAMLOPS_POSTGRES_POOL=0` must be a real escape hatch, not a no-op."""
    from examlops.storage import pg as pgmod

    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL", "0")
    pgmod.close_pools()

    pids = set()
    for _ in range(5):
        conn = pgmod.connect(DSN)
        pids.add(conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"])
        conn.close()
    assert len(pids) == 5


def test_pooled_connections_serve_concurrent_writers(pg_schema, monkeypatch):
    """The reason the seam exists at all: SQLite allows one writer, Postgres does not.

    Sixteen threads write through the unmodified audit helper at once. Every row must land, and the
    hash chain — which is what a shared connection would corrupt — must still verify.
    """
    import threading

    from examlops.data.audit import export_audit_events, verify_audit_chain, write_audit_event

    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL", "1")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_POOL_MAX", "8")
    from examlops.storage import pg as pgmod

    pgmod.close_pools()
    errors: list[BaseException] = []

    def _write(i: int) -> None:
        try:
            write_audit_event("test", "pool", "pool_test", f"m{i}")
        except BaseException as exc:  # noqa: BLE001 - surfaced below so the test names the failure
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, errors
        rows = [e for e in export_audit_events() if e["action"] == "pool_test"]
        assert len(rows) == 16
        assert verify_audit_chain()["ok"] is True
    finally:
        pgmod.close_pools()
