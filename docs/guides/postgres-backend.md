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
pip install 'examlops[postgres]'          # brings in psycopg + psycopg-pool

export EXAMLOPS_DB_BACKEND=postgres
export EXAMLOPS_POSTGRES_DSN='postgresql://examlops:…@db.example.org:5432/examlops'

exa status                                 # creates the schema on first use
```

### Several instances on one server

`EXAMLOPS_POSTGRES_SCHEMA` scopes the whole platform to one Postgres schema, created on demand —
the equivalent of pointing `PLATFORM_DB` at a different file. Unset, everything lands in `public`.

```bash
EXAMLOPS_POSTGRES_SCHEMA=staging exa status     # staging's own tables, same database
```

The test suite uses this to keep one Postgres server for every test run.

Every process that touches platform state — CLI, control plane, dashboard, SeanerBUS bridge, agent —
must carry both variables. A process that carries only one of them silently keeps using
`platform.db`, which is the failure mode to watch for: split state, not an error message.

The schema is created on demand (127 tables, `CREATE TABLE IF NOT EXISTS`), so pointing at an empty
database is all the provisioning there is.

### Connection pooling

Connections are pooled per `(DSN, schema)` per process, and you do not have to do anything to get
it. It matters because `get_db()` opens a connection **per call** — free on SQLite, a TCP round
trip plus a backend fork on Postgres, and a single dashboard page render makes dozens. Measured
against a local Postgres 16, 200 × open→query→close:

| | per call |
|---|---|
| pooled | **3.3 ms** |
| unpooled | 28.1 ms |

That gap is a lower bound: it is all connection setup, so it grows with network latency and with
TLS.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_POSTGRES_POOL` | `1` | `0`/`false`/`off` opens an unpooled connection per call |
| `EXAMLOPS_POSTGRES_POOL_MIN` | `1` | connections kept open |
| `EXAMLOPS_POSTGRES_POOL_MAX` | `10` | ceiling per process — **multiply by your process count** and keep it under the server's `max_connections` |

`psycopg-pool` is part of the `postgres` extra. If it is missing the platform opens an unpooled
connection instead of failing — slower, still correct. Pools are closed at interpreter exit, so a
short-lived `exa` command does not hang on them.

## How it works

Every one of the platform's ~252 datastore helpers opens its connection through a single function,
`platform_db.get_db()`. The Postgres backend returns a connection that speaks the same
`sqlite3`-shaped API and **translates the SQL on the way through**
(`examlops/storage/pg.py`) — placeholders, `AUTOINCREMENT`, `DATETIME` defaults,
`INSERT OR REPLACE`, `PRAGMA table_info`, `BEGIN IMMEDIATE`, SQLite's date functions, and
`sqlite_master` (supplied as a subquery over `pg_tables`/`pg_indexes`, because ~20 call sites ask
it whether an optional table exists yet).

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

- Verified: **the whole unit suite runs on Postgres 16** — 2090 passed, 15 skipped, 0 failed
  (`make test-postgres`), alongside the dedicated live-backend suite
  (`tests/integration/test_postgres_backend_live.py`, 13 tests) covering schema creation, the audit
  hash chain, append-only enforcement, upserts, timestamps, `lastrowid`, and pooled concurrent
  writers.
- The 15 skips are honest, not hidden: 10 SQLite-backup-tier tests, 4 schema-once-bootstrap tests
  (both keyed to a `platform.db` **file**), and one unrelated pre-existing skip. Each names its
  reason. (The dashboard-reader test that used to skip here now runs on both engines — it is the
  guard on the connection change below.)
- **Backups cover the platform datastore on this engine** — see [Backing it up](#backing-it-up)
  below. The 10 skipped backup tests are the SQLite *file*-snapshot tests, which have nothing to
  snapshot here; the Postgres path has its own 18 tests
  (`tests/unit/test_backup_platform_datastore.py`).
- **The dashboard reads the configured engine** — `dbconn.connect()` ignores the SQLite path it is
  handed and opens Postgres when `EXAMLOPS_DB_BACKEND=postgres`, so the consoles and the CLI can no
  longer end up on different stores. It needs `platform/cli/src` on `PYTHONPATH` (the container sets
  this); if the import fails it keeps serving on SQLite and logs an error saying it is not reading
  platform state, rather than failing to start.
- The dashboard's **own** test suite is still SQLite-shaped and remains a SQLite-tier gate: 34 of its
  modules seed a temporary `platform.db` file with raw `sqlite3`, so on Postgres they assert against
  a store the routers no longer read (450 pass on SQLite; 338/450 on Postgres, all remaining failures
  fixture-shaped, no engine errors). Porting those fixtures to the seam is a tracked step.
- `exa data retention-prune --vacuum` and `exa doctor`'s DB checks are SQLite-specific.

Progress and the remaining work are tracked in `.claude/plans/enterprise-readiness/05-POSTGRES-MIGRATION.md`.

## Backing it up

On this engine `platform.db` is an empty leftover file: every helper writes to Postgres. A bundle
that snapshots that file looks complete and restores nothing, so `exa backup` hands the platform
datastore over to the Postgres tier:

```bash
exa backup create --with-postgres        # or --all
```

Bare `exa backup create` **refuses** here rather than writing a hollow bundle:

```
✗ EXAMLOPS_DB_BACKEND=postgres: platform state is in Postgres, not in platform.db.
  Use 'exa backup create --with-postgres' (or --all) to dump it.
```

What that produces, and how it is scoped:

| | |
|---|---|
| Dump | `postgres/platform.dump`, `pg_dump -Fc` (custom format: compressed, selectively restorable) |
| Connection | from `EXAMLOPS_POSTGRES_DSN`, split into `PG*` env vars — the password never reaches `argv`, so it is not visible in `ps` |
| Scope | `--schema $EXAMLOPS_POSTGRES_SCHEMA` when that is set, so one database holding several instances backs up only its own |
| SQLite tier | records `platform` as `skipped`, with the reason, instead of snapshotting the empty file |

Restore is guarded by `--force`, because `pg_restore --clean --if-exists` drops objects first:

```bash
exa backup restore-bundle ./backups/examlops-backup-<stamp> --tier postgres --force --yes
```

The restore reconnects from the **current** `EXAMLOPS_POSTGRES_DSN`, not from the database name the
dump was taken against — restoring into a standby is a config change, not a different command. It
exits non-zero and names every failed item if anything did not come back; a restore that failed and
reported success is worse than one that raised.

`pg_dump`/`pg_restore` must be on `PATH` (`postgresql-client`). If they are not, or the server is
unreachable, the tier degrades to `skipped` with a reason and the rest of the bundle still succeeds
— which also means a bundle can be `ok` with no platform dump in it. `exa backup verify-bundle`
shows what a bundle actually contains.

## Run the tests yourself

```bash
make test-postgres        # starts a throwaway Postgres, runs the unit suite on it, cleans up
```

That is the parity check: the same unit suite the SQLite path runs, executed against Postgres.
It also runs the dedicated live-backend suite. To drive it by hand:

```bash
docker run -d --name examlops-pgtest \
  -e POSTGRES_PASSWORD=examlops -e POSTGRES_USER=examlops -e POSTGRES_DB=examlops \
  -p 15433:5432 postgres:16-alpine

EXAMLOPS_DB_BACKEND=postgres EXAMLOPS_POSTGRES_SCHEMA=exa_test \
EXAMLOPS_POSTGRES_DSN='postgresql://examlops:examlops@localhost:15433/examlops' \
  .venv/bin/pytest tests/unit/ -q

EXAMLOPS_POSTGRES_TEST_DSN='postgresql://examlops:examlops@localhost:15433/examlops' \
  .venv/bin/pytest tests/integration/test_postgres_backend_live.py -v
```

The live-backend suite **drops and recreates the `public` schema**, which is why it reads a
dedicated `EXAMLOPS_POSTGRES_TEST_DSN` and never the DSN your platform runs on. Under
`EXAMLOPS_DB_BACKEND=postgres` the unit suite truncates between tests (`tests/conftest.py`), so
point it at a throwaway database too.
