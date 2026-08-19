"""SQLite→Postgres statement translation (enterprise-readiness item 0.1 follow-on).

These are pure-function tests: no driver, no server, so they run in CI on every push. The live
round-trip against a real Postgres lives in ``tests/integration/test_postgres_backend_live.py``.

What is being protected here is the property that makes the migration affordable: the platform's
252 helpers reach the datastore through one chokepoint, so translating at that seam means none of
them has to change. Every case below is a construct taken from the real schema or a real helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "cli" / "src"))

from examlops.storage.pg import PgRow, split_script, translate  # noqa: E402


def test_placeholders_become_pyformat():
    assert translate("SELECT * FROM t WHERE a = ? AND b = ?") == (
        "SELECT * FROM t WHERE a = %s AND b = %s"
    )


def test_string_literals_are_never_rewritten():
    """A '?' inside a quoted string is data, not a placeholder."""
    out = translate("SELECT * FROM t WHERE note = 'why? because' AND x = ?")
    assert out == "SELECT * FROM t WHERE note = 'why? because' AND x = %s"


def test_ddl_types_and_autoincrement():
    out = translate(
        "CREATE TABLE IF NOT EXISTS x (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, v REAL, b BLOB)"
    )
    assert "id BIGSERIAL PRIMARY KEY" in out
    assert "ts TEXT" in out  # timestamps stay text — 252 helpers treat them as strings
    assert "v DOUBLE PRECISION" in out
    assert "b BYTEA" in out
    assert "AUTOINCREMENT" not in out


def test_current_timestamp_keeps_sqlite_text_shape():
    out = translate("SELECT CURRENT_TIMESTAMP")
    assert "to_char" in out and "YYYY-MM-DD HH24:MI:SS" in out


def test_insert_or_replace_uses_the_tables_key():
    out = translate(
        "INSERT OR REPLACE INTO traffic_rules (model, production, canary) VALUES (?,?,?)",
        pk_lookup=lambda t: ["model"],
    )
    assert "ON CONFLICT (model) DO UPDATE SET production=EXCLUDED.production" in out
    assert "canary=EXCLUDED.canary" in out


def test_insert_or_replace_without_a_key_degrades_to_insert():
    """Guessing a conflict target would silently corrupt upsert semantics — so it does not."""
    out = translate("INSERT OR REPLACE INTO t (a) VALUES (?)", pk_lookup=lambda t: [])
    assert "ON CONFLICT" not in out
    assert out.startswith("INSERT INTO t")


def test_insert_or_ignore():
    assert translate("INSERT OR IGNORE INTO t (a) VALUES (?)").endswith("ON CONFLICT DO NOTHING")


def test_pragma_table_info_is_shaped_like_the_pragma():
    out = translate("PRAGMA table_info(audit_events)")
    assert "column_name AS name" in out and "information_schema.columns" in out
    assert "'audit_events'" in out


def test_other_pragmas_are_no_ops():
    assert translate("PRAGMA journal_mode=WAL") == "SELECT 1"


def test_begin_immediate_becomes_an_advisory_lock():
    """The audit hash-chain's cross-process write mutex must survive the port."""
    assert "pg_advisory_xact_lock" in translate("BEGIN IMMEDIATE")


def test_julianday_difference_becomes_epoch_seconds():
    out = translate(
        "UPDATE t SET x = 1 WHERE (julianday(CURRENT_TIMESTAMP) - julianday(last)) * 86400.0 >= ?"
    )
    assert "EXTRACT(EPOCH FROM" in out and "julianday" not in out


def test_datetime_now_modifier():
    out = translate("DELETE FROM t WHERE ts < datetime('now', ?)")
    assert "CAST(%s AS interval)" in out and "datetime(" not in out


def test_reserved_identifier_is_quoted():
    """`window` is the one platform column name Postgres reserves."""
    assert '"window"' in translate("SELECT window FROM slo_burn")


def test_comments_are_not_split_or_rewritten():
    script = "-- a comment; with a semicolon and a ? mark\nCREATE TABLE a (x TEXT);"
    stmts = split_script(script)
    assert len(stmts) == 1
    assert "? mark" in translate(stmts[0])  # prose left alone


def test_trigger_body_stays_one_statement_and_ports():
    script = (
        "CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only (D4)'); END;"
    )
    stmts = split_script(script)
    assert len(stmts) == 1
    out = translate(stmts[0])
    assert "CREATE OR REPLACE FUNCTION" in out and "RAISE EXCEPTION" in out
    assert "audit_events is append-only (D4)" in out


def test_row_is_addressable_by_name_and_position():
    row = PgRow({"a": 1, "b": 2})
    assert row["a"] == 1
    assert row[1] == 2  # sqlite3.Row semantics the helpers rely on
