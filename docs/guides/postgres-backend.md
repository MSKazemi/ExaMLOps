# Postgres datastore backend

ExaMLOps stores its operational state — audit log, drift snapshots, traffic rules, projects, costs,
evaluations, HPC jobs — in one shared database reached through `examlops.platform_db`. The default
engine is **SQLite** (`platform.db`), which is ideal for a laptop, a single node, and CI, and which
allows exactly **one writer at a time**. Setting `EXAMLOPS_DB_BACKEND=postgres` moves that state to
**Postgres** without changing a line of platform code.

## When you need it

| Signal | Why SQLite runs out |
|---|---|
| More than one host runs `exa`, the control plane, the bridge or the dashboard | SQLite's single writer is process-local; a shared file over NFS is not a substitute |
| You want HA, streaming backup, or point-in-time recovery | there is one file, and it is the SPOF |
| You need per-tenant isolation or row-level security | SQLite has neither |
| Write contention shows up as `database is locked` | WAL + busy-timeout buys headroom, not scale |

Below those thresholds, stay on SQLite. It is the tested default and it is faster for one node.

## Enable it

```bash
pip install 'examlops[postgres]'          # brings in psycopg

export EXAMLOPS_DB_BACKEND=postgres
export EXAMLOPS_POSTGRES_DSN='postgresql://examlops:…@db.example.org:5432/examlops'

exa status                                 # creates the schema on first use
```

Every process that touches platform state — CLI, control plane, dashboard, SeanerBUS bridge, agent —
must carry both variables. A process that carries only one of them silently keeps using
`platform.db`, which is the failure mode to watch for: split state, not an error message.

The schema is created on demand (127 tables, `CREATE TABLE IF NOT EXISTS`), so pointing at an empty
database is all the provisioning there is.

## How it works

Every one of the platform's ~252 datastore helpers opens its connection through a single function,
`platform_db.get_db()`. The Postgres backend returns a connection that speaks the same
`sqlite3`-shaped API and **translates the SQL on the way through**
(`examlops/storage/pg.py`) — placeholders, `AUTOINCREMENT`, `DATETIME` defaults,
`INSERT OR REPLACE`, `PRAGMA table_info`, `BEGIN IMMEDIATE`, and SQLite's date functions.

Two consequences worth knowing:

- **Timestamps remain text** in the SQLite format (`2026-08-19 20:24:23`, UTC). Helpers compare and
  return them as strings, so the port keeps that shape rather than silently changing 252 return
  types. Use `to_timestamp(ts, 'YYYY-MM-DD HH24:MI:SS')` in your own SQL if you need a real
  timestamp.
- **The audit log's tamper-evidence survives.** The append-only guards on `audit_events` become
  Postgres triggers, and the hash chain's cross-process write lock becomes
  `pg_advisory_xact_lock`. `exa audit verify` behaves identically on both engines.

## Migrating existing state

There is no automatic SQLite→Postgres data copy yet. For a fresh deployment, point at Postgres from
the start. For an existing one, treat it as a migration project: export what you need
(`exa audit export`, the FinOps cost tables) and re-import. The audit hash chain cannot be
transplanted row-by-row without re-chaining, so plan a checkpoint (`exa audit checkpoint`) before
the cut-over and keep the SQLite file as the archival record of everything before it.

## Current limits

Honest status, so you can decide whether this fits your deployment:

- Verified: full schema creation, the audit hash chain, append-only enforcement, upserts,
  timestamps, `lastrowid`, project and drift helpers — `tests/integration/test_postgres_backend_live.py`
  (10 tests) against Postgres 16.
- **Not yet verified: the full unit suite on Postgres.** It needs per-test schema isolation first.
- **No connection pooling yet** — one connection per `get_db()` call. Fine for CLI use, not for a
  high-QPS service.
- The **backup tier is SQLite-only** (`exa backup`). A Postgres deployment needs `pg_dump` in its
  own backup path until that lands.
- `exa data retention-prune --vacuum` and `exa doctor`'s DB checks are SQLite-specific.

Progress and the remaining work are tracked in `.claude/plans/enterprise-readiness/05-POSTGRES-MIGRATION.md`.

## Run the live tests yourself

```bash
docker run -d --name examlops-pgtest \
  -e POSTGRES_PASSWORD=examlops -e POSTGRES_USER=examlops -e POSTGRES_DB=examlops \
  -p 15433:5432 postgres:16-alpine

EXAMLOPS_POSTGRES_TEST_DSN='postgresql://examlops:examlops@localhost:15433/examlops' \
  .venv/bin/pytest tests/integration/test_postgres_backend_live.py -v
```

That suite **drops and recreates the `public` schema**, which is why it reads a dedicated
`EXAMLOPS_POSTGRES_TEST_DSN` and never the DSN your platform runs on.
