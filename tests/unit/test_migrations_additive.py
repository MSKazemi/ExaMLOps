"""Additive-only schema migrations guard (enterprise-readiness Phase 1, item 1.10).

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
    src = inspect.getsource(pdb.init_db)
    hits = _DESTRUCTIVE.findall(src)
    assert not hits, f"init_db contains non-rolling-safe DDL: {hits}"
    # Tables/indexes must be created idempotently so an in-flight old replica isn't disrupted.
    assert "CREATE TABLE IF NOT EXISTS" in src


def test_migration_columns_have_defaults_or_are_nullable():
    """A rolling-safe ADD COLUMN must be nullable or defaulted (old writers omit it)."""
    for table, cols in pdb._COLUMN_MIGRATIONS.items():
        for name, decl in cols.items():
            d = decl.upper()
            ok = ("DEFAULT" in d) or ("NOT NULL" not in d)
            assert ok, (
                f"{table}.{name} is NOT NULL without a DEFAULT — breaks old writers mid-rollout"
            )
