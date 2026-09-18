"""Additive-only schema migrations guard (enterprise-readiness Phase 1, item 1.10).

It also guards the *other* half of the same contract, added after live finding D1: a column added
to a DDL block of a table that can already exist is invisible to ``CREATE TABLE IF NOT EXISTS``, so
it reaches an existing database only through ``_COLUMN_MIGRATIONS``. See
:data:`_FIRST_RELEASE_COLUMNS`.

Zero-downtime rolling upgrades (and `helm rollback`) only work if a new schema is still readable by
the *previous* app version — i.e. migrations follow expand/contract and never do a destructive,
non-reversible change in the same release. This guard enforces that invariant on the SQLite schema
path so a rolling upgrade can't be broken by a `DROP COLUMN` / `DROP TABLE` / `RENAME` slipping in:

  * `_migrate_columns` may only ADD columns (the mechanism itself only emits `ADD COLUMN`);
  * the `init_db` DDL uses `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` only —
    no destructive DDL that an older replica couldn't tolerate mid-rollout.
"""

from __future__ import annotations

import inspect
import re

import examlops.platform_db as pdb

_DESTRUCTIVE = re.compile(r"\b(DROP\s+TABLE|DROP\s+COLUMN|RENAME\s+COLUMN|RENAME\s+TO)\b", re.I)


def test_column_migrations_are_add_only():
    # Every migration declaration is a column add (name -> type decl); the runner only ALTERs ADD.
    src = inspect.getsource(pdb._migrate_columns)
    assert "ADD COLUMN" in src
    assert not _DESTRUCTIVE.search(src), "destructive DDL in the column-migration runner"


def test_init_db_ddl_has_no_destructive_statements():
    # init_db takes the per-process lock; the DDL itself runs in _bootstrap_schema.
    src = inspect.getsource(pdb.init_db) + inspect.getsource(pdb._bootstrap_schema)
    hits = _DESTRUCTIVE.findall(src)
    assert not hits, f"init_db contains non-rolling-safe DDL: {hits}"
    # Tables/indexes must be created idempotently so an in-flight old replica isn't disrupted.
    assert "CREATE TABLE IF NOT EXISTS" in src


# ── a column added to an existing table's DDL needs a migration (live finding D1) ────────────
#
# `init_db` only creates *missing* tables, so a column added to a `CREATE TABLE IF NOT EXISTS`
# block never appears on a database that already has that table. `dataplane_streams.state_reason`
# was added mid-batch and had no migration entry: on the one instance whose table predated it,
# every pause, resume, disable and pack-removal sweep failed with `no such column` — a 500 with a
# traceback, on the only route that can pause a live stream.
#
# The rule this guard enforces, for the tables named below: every column in the DDL is either one
# the table's FIRST RELEASED shape had, or has an entry in `_COLUMN_MIGRATIONS`. Adding a column
# therefore fails here until the migration is written.
#
# Extending a baseline is legitimate in exactly one case — a table no release has ever created, so
# no database can hold an older shape of it. Anything else must be a migration entry: a baseline
# edited to make this test pass is a promise that no installation has the old shape, and it is
# checked by nothing but the person making it.
_FIRST_RELEASE_COLUMNS: dict[str, frozenset[str]] = {
    # ADR 0130 (Plan 1, released): unchanged since they were created.
    "dataplane_sources": frozenset(
        {
            "project",
            "name",
            "connector",
            "connection",
            "spec_json",
            "schedule",
            "limits_json",
            "contract",
            "enabled",
            "created_by",
            "created_at",
            "updated_at",
        }
    ),
    "dataplane_pulls": frozenset(
        {
            "id",
            "project",
            "source",
            "status",
            "trigger_kind",
            "actor",
            "started_at",
            "finished_at",
            "revision",
            "parent_revision",
            "row_count",
            "byte_count",
            "watermark_json",
            "error",
        }
    ),
    # ADR 0131 task A6. `state_reason` is deliberately NOT here: it was added to this block after
    # the table had already been created on a running instance, which is the defect this guard
    # exists for. It reaches such an instance through `_COLUMN_MIGRATIONS`.
    "dataplane_streams": frozenset(
        {
            "project",
            "name",
            "connector",
            "model",
            "alias",
            "address",
            "connection",
            "options_json",
            "limits_json",
            "state",
            "origin",
            "schema_version",
            "created_by",
            "created_at",
            "updated_at",
        }
    ),
    # ADR 0131 task A7b: created whole, in one change, and unchanged since.
    "dataplane_stream_dead_letters": frozenset(
        {
            "id",
            "project",
            "stream",
            "reason",
            "error",
            "attempts",
            "sha256",
            "size",
            "origin_json",
            "origin_topic",
            "origin_partition",
            "origin_offset",
            "payload",
            "payload_encoding",
            "payload_truncated",
            "created_at",
            "updated_at",
            "replayed_at",
            "replayed_by",
            "replay_claim",
            "replay_claimed_at",
        }
    ),
}

#: Words that begin a table *constraint* line rather than a column definition.
_NOT_A_COLUMN = {"primary", "unique", "foreign", "check", "constraint"}


def _ddl_columns(table: str) -> list[str]:
    """The column names of ``table``'s ``CREATE TABLE`` block in the live ``init_db`` DDL."""
    src = inspect.getsource(pdb._bootstrap_schema)
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\s*\);", src, re.S | re.I
    )
    assert match, f"no CREATE TABLE block for {table} — the guard's parser is stale, not the DDL"
    columns: list[str] = []
    depth = 0
    for raw in match.group(1).splitlines():
        line = raw.split("--", 1)[0].strip()
        if not line:
            continue
        first = line.split()[0].strip("(,").lower()
        if depth == 0 and first not in _NOT_A_COLUMN and re.fullmatch(r"[a-z_][a-z0-9_]*", first):
            columns.append(first)
        depth += line.count("(") - line.count(")")
    assert columns, f"parsed no columns out of {table}'s DDL"
    return columns


def test_every_guarded_ddl_column_shipped_with_the_table_or_has_a_migration():
    offenders: list[str] = []
    for table, baseline in _FIRST_RELEASE_COLUMNS.items():
        migrated = set(pdb._COLUMN_MIGRATIONS.get(table, {}))
        for column in _ddl_columns(table):
            if column not in baseline and column not in migrated:
                offenders.append(f"{table}.{column}")
    assert not offenders, (
        "these columns are in a CREATE TABLE IF NOT EXISTS block of a table that can already "
        "exist, and nothing adds them to an existing database — add an entry to "
        f"platform_db._COLUMN_MIGRATIONS for each: {', '.join(offenders)}"
    )


def test_the_guarded_baselines_still_describe_the_live_ddl():
    """A baseline column that left the DDL is a baseline nobody has read since it was written."""
    for table, baseline in _FIRST_RELEASE_COLUMNS.items():
        gone = baseline - set(_ddl_columns(table))
        assert not gone, f"{table}: {sorted(gone)} is in the baseline but not in the DDL"


def test_a_migration_entry_names_a_column_the_ddl_declares():
    """The mirror image: a migration for a column the DDL does not have would create it on an old
    database and never on a new one — two shapes of the same table, drifting apart."""
    for table in _FIRST_RELEASE_COLUMNS:
        declared = set(_ddl_columns(table))
        for column in pdb._COLUMN_MIGRATIONS.get(table, {}):
            assert column in declared, f"{table}.{column} is migrated but absent from the DDL"


def test_migration_columns_have_defaults_or_are_nullable():
    """A rolling-safe ADD COLUMN must be nullable or defaulted (old writers omit it)."""
    for table, cols in pdb._COLUMN_MIGRATIONS.items():
        for name, decl in cols.items():
            d = decl.upper()
            ok = ("DEFAULT" in d) or ("NOT NULL" not in d)
            assert ok, (
                f"{table}.{name} is NOT NULL without a DEFAULT — breaks old writers mid-rollout"
            )
