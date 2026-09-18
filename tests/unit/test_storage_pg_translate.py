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


def test_placeholder_in_a_boolean_position_is_compared_not_coerced():
    """`CASE WHEN ? THEN` gets a 0/1 int (bools are adapted); Postgres wants a boolean there."""
    out = translate("INSERT INTO t (a, b) VALUES (?, CASE WHEN ? THEN CURRENT_TIMESTAMP END)")
    assert "CASE WHEN %s <> 0 THEN" in out
    # An expression already yielding a boolean is left alone.
    assert "WHEN x IS NULL THEN" in translate("SELECT CASE WHEN x IS NULL THEN 1 ELSE 0 END FROM t")


def test_sqlite_master_becomes_the_postgres_catalogue():
    """~20 call sites ask sqlite_master whether an optional table exists yet."""
    out = translate("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?")
    assert "pg_tables" in out and "pg_indexes" in out and "AS sqlite_master" in out
    assert "type='table'" in out  # the predicate is untouched — the catalogue supplies `type`


def test_rowid_tiebreak_is_dropped_for_a_table_that_has_no_id():
    """`judge_calibrations` keys on a natural TEXT id, so there is no surrogate to order by."""
    sql = "SELECT * FROM judge_calibrations ORDER BY ts DESC, rowid DESC LIMIT ?"
    assert "id DESC" in translate(sql, has_id=lambda t: True)
    out = translate(sql, has_id=lambda t: False)
    assert "rowid" not in out and "id DESC" not in out and "ORDER BY ts DESC LIMIT %s" in out


def test_julianday_difference_becomes_epoch_seconds():
    out = translate(
        "UPDATE t SET x = 1 WHERE (julianday(CURRENT_TIMESTAMP) - julianday(last)) * 86400.0 >= ?"
    )
    assert "EXTRACT(EPOCH FROM" in out and "julianday" not in out


def test_datetime_now_modifier():
    out = translate("DELETE FROM t WHERE ts < datetime('now', ?)")
    assert "CAST(%s AS interval)" in out and "datetime(" not in out


def test_datetime_over_current_timestamp():
    """The lease/cooldown/rate-limit form: datetime(CURRENT_TIMESTAMP, '-30 seconds')."""
    out = translate("SELECT 1 WHERE started_at <= datetime(CURRENT_TIMESTAMP, ?)")
    assert "datetime(" not in out
    assert "CAST(%s AS interval)" in out
    assert out.count("to_char") == 2  # inner CURRENT_TIMESTAMP, outer result


def test_rowid_becomes_the_serial_id():
    """SQLite's implicit rowid is an insertion-order tie-break; `id` carries the same meaning."""
    out = translate("SELECT * FROM t ORDER BY ts DESC, rowid DESC LIMIT ?")
    assert "ORDER BY ts DESC, id DESC" in out
    assert "rowid" not in out


def test_lastrowid_word_is_not_mangled():
    assert "lastrowid" in translate("SELECT lastrowid FROM t")


def test_drop_trigger_gets_its_table():
    out = translate(
        "DROP TRIGGER IF EXISTS audit_events_no_update", trigger_table=lambda n: "audit_events"
    )
    assert out == "DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events"


def test_drop_trigger_if_exists_on_unknown_trigger_is_a_no_op():
    assert translate("DROP TRIGGER IF EXISTS nope", trigger_table=lambda n: None) == "SELECT 1"


def test_pragma_table_info_is_scoped_to_the_active_schema():
    """Two instances in one database must not see each other's columns, or migrations skip."""
    assert "current_schema()" in translate("PRAGMA table_info(audit_events)")


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


def test_a_row_slices_like_sqlite3_row():
    """``row[3:]`` raised KeyError on Postgres and worked on SQLite, so POST /v1/retrain answered
    500 on every Postgres control plane while the SQLite suite stayed green."""
    from examlops.storage.pg import PgRow

    values = (1, 2, 3, 4)  # sqlite3.Row slices to a tuple of the selected columns
    row = PgRow(a=1, b=2, c=3, d=4)
    for key in (slice(1, None), slice(None, 2), slice(1, 3), slice(None, None, -1)):
        assert row[key] == values[key]
        assert isinstance(row[key], tuple)
    assert row[0] == 1 and row["c"] == 3


def test_a_column_added_by_a_migration_gets_a_type_postgres_has():
    """`ALTER TABLE … ADD COLUMN` carries a SQLite type, and it was not being translated.

    The consequence was total: `platform_db._COLUMN_MIGRATIONS` gained
    `project_budgets.alerted_at DATETIME`, Postgres has no such type, and every process that opens
    the datastore died in its first `init_db()` —
    `psycopg.errors.UndefinedObject: type "datetime" does not exist`. The control plane crash-looped
    on startup, so a Postgres install was not degraded, it was down. Found by a kind drill whose
    chart install would not come up.

    The quieter half is the other two rows: a column added this way was int4 and float4 rather than
    the bigint and double precision the schema means everywhere else.
    """
    assert translate("ALTER TABLE project_budgets ADD COLUMN alerted_at DATETIME").endswith("TEXT")
    assert translate("ALTER TABLE gateway_calls ADD COLUMN latency_ms REAL").endswith(
        "DOUBLE PRECISION"
    )
    assert translate("ALTER TABLE gateway_calls ADD COLUMN error INTEGER").endswith("BIGINT")
    assert translate("ALTER TABLE slo_samples ADD COLUMN watermark TEXT").endswith("TEXT")


def test_every_column_migration_in_the_schema_translates():
    """The table above is the real input, so the guard follows it rather than a copy: a migration
    added with a type Postgres does not have must fail here, not in production's first startup."""
    from examlops.platform_db import _COLUMN_MIGRATIONS

    postgres_types = ("TEXT", "BIGINT", "DOUBLE PRECISION", "BYTEA", "BOOLEAN", "NUMERIC")
    for table, columns in _COLUMN_MIGRATIONS.items():
        for name, decl in columns.items():
            out = translate(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            assert out.startswith(f"ALTER TABLE {table} ADD COLUMN {name} "), out
            kind = out.split(f"ADD COLUMN {name} ", 1)[1].split(" DEFAULT")[0].strip()
            assert kind.startswith(postgres_types), f"{table}.{name} translates to {kind!r}"
