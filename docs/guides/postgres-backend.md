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

Every process that touches platform state — CLI, control plane, dashboard, Dataplane bus bridge, agent —
must carry both variables. A process that carries only one of them silently keeps using
`platform.db`, which is the failure mode to watch for: split state, not an error message.

The schema is created on demand (142 tables, `CREATE TABLE IF NOT EXISTS`), so pointing at an empty
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
| `EXAMLOPS_POSTGRES_CONNECT_TIMEOUT` | `2.0` | seconds to wait for the server to answer *at all* before declaring it unreachable — see below |
| `EXAMLOPS_POSTGRES_UNREACHABLE_TTL` | `5.0` | seconds an unreachable verdict is remembered, so one command probes once |

#### Never open the datastore inside a loop

The corollary, and the one performance mistake this engine punishes that SQLite does not. A loop
that opens a connection per iteration is nearly free on a local file and costs a pool checkout plus
a round trip per iteration here. Measured before it was fixed, `corruption.input_drift_rows()` over
50 models opened **101 connections** (the model list, then each model's snapshots and each model's
baseline) and took 24 ms; sharing one connection made it **2 connections and 11 ms** for identical
output — against a server on the *same host*, which is the smallest that gap ever gets.

```python
# wrong — one connection per model
for model in models:
    with get_db() as conn:
        conn.execute(...)

# right — one connection for the report, passed to the helpers it calls
with get_db() as conn:
    for model in models:
        conn.execute(...)
        baseline = get_input_baseline(model, conn=conn)
```

Platform helpers that a loop is likely to call take an optional `conn=` for exactly this.

`tests/unit/test_no_connection_per_iteration.py` is an AST walk that fails on a new site, because
this is a defect that behaves like correct code — every test still passes, only slower. It matches
both spellings, `with get_db() as conn:` **and** `conn = get_db()`; it originally saw only the
context-manager form, which is how two sites in the control plane went unnoticed until the guard
itself was re-examined.

Three files legitimately open per iteration, and each would be a *defect* if hoisted:

| File | Why a connection per iteration is right |
|---|---|
| `lifecycle/upgrade.py` | each migration gets its own transaction, so one that fails cannot roll back the migrations already applied |
| `telemetry_anchor.py` | the connection is released *before* `write_audit_event` opens its own — holding one across a nested open is how a small pool deadlocks |
| `control_plane/app.py` | the datastore open is itself retried (a fresh connection per attempt is the point), and the reconciler gives each run its own transaction so one failure cannot roll back the runs before it, on the control plane's own SQLite store where a connection is nearly free |

Each exemption carries **how many** such sites its file may have, so a new one in an already-exempted
file still fails the guard rather than inheriting somebody else's justification.

#### And do not issue one query per item either

The same shape one level down: a loop that *reuses* its connection but runs a query per model is
still one round trip per model. The dashboard's drift panel issued **21 statements for 10 models and
101 for 50** — one for each model's window, one for each model's baseline.

**The obvious fix is right on this engine and wrong on the other**, which is worth knowing before
you reach for it. Reading every model's newest rows in one windowed statement:

```sql
SELECT model, prediction FROM (
  SELECT model, prediction,
         ROW_NUMBER() OVER (PARTITION BY model ORDER BY ts DESC, rowid DESC) AS rn
  FROM drift_snapshots
) ranked WHERE rn <= ?
```

Measured over 250 000 snapshots across 50 models, three runs each:

| | one statement per model | one windowed statement |
|---|---|---|
| SQLite | **47 ms** | 129 ms |
| Postgres | 134–148 ms | **70 ms** |

The window function has to rank every row in the table before keeping the newest few per model,
while a per-model query walks the `(model, ts)` index and stops after its window. On SQLite, where a
statement is an in-process call, that makes the per-model form **2.7× faster**; on Postgres the 50
round trips cost more than the ranking, and the single statement is **2× faster**. Both queries are
portable — SQLite has had window functions since 3.25 and `rowid` is translated for Postgres — so
this is purely a cost decision, and `routers/drift_data.py` makes it on the engine with both numbers
recorded beside the code.

The lesson generalises past this one route: **fewer statements is not the same as less work.** Count
round trips when they are the cost, and measure at the size you actually run at — a fixture with a
dozen rows per model will tell you the opposite of the truth.

N+1 is a shape no linter can judge — a loop over six fixed tables is fine, a loop over every model
is not — so the guard is *behavioural*:
`platform/services/dashboard/backend/tests/test_drift_reads_do_not_scale.py` measures each route at
5 models and at 50 and asserts what is correct for the configured engine: a constant statement count
on Postgres, and on SQLite a count that grows by no more than one read per model. It also asserts
the route returned the rows it seeded, so it cannot pass by measuring a route that silently returned
nothing.

#### If the dashboard starts hanging, suspect a connection that was never returned

Pooling changes what an unreleased connection costs. Unpooled — and on SQLite — a connection you
forget is collected with the object, and the price is a file handle. Pooled, it is *gone*: the pool
counts it as checked out for the life of the process. Reach `EXAMLOPS_POSTGRES_POOL_MAX` and every
later request waits `psycopg_pool`'s full 30-second budget for a connection that is never coming
back, so the symptom is a dashboard that hangs, then times out, with a perfectly healthy server.

The platform's own code is guarded against this — every connection it opens is released in a
`finally`, and two tests hold that line: `test_connections_are_scoped.py` reads the source and
fails on any unguarded site, and `test_pool_survives_error_paths.py` shrinks the pool to two and
makes a handler fail more times than that. If you are writing code that opens the datastore
directly, do the same:

```python
conn = connect(db_path)
try:
    ...
finally:
    conn.close()
```

Note that `with sqlite3.connect(...) as conn:` does **not** help — that context manager commits or
rolls back a transaction, it does not close the connection.

If you need to confirm a leak rather than guess at one, count checked-out backends on the server:

```sql
SELECT state, count(*) FROM pg_stat_activity WHERE datname = current_database() GROUP BY state;
```

A pile of `idle in transaction` or a count pinned at exactly your `POOL_MAX` × process count is the
tell.

### When the server is not there

Reachability and pool saturation are different failures and get different budgets.

Before a process builds its first pool, the platform makes one bounded TCP probe at the DSN's
host and port. If nothing is listening it raises immediately, naming the address it tried:

```
warning: platform datastore unavailable — unreachable at 10.0.0.5:5432 after 2s
  ([Errno 111] Connection refused). Check EXAMLOPS_POSTGRES_DSN and that the server is
  running; raise EXAMLOPS_POSTGRES_CONNECT_TIMEOUT if the host is simply slow.
```

Without that probe an unreachable server costs **the pool's** timeout — 30 seconds — on the first
connection of every process, because the pool's background workers keep retrying a refused connect
while `getconn()` waits out its full budget. On the CLI, where each command is a fresh process,
that was 30 s *per command*, paid most by whoever was diagnosing the outage. It is now under a
second.

Raise `EXAMLOPS_POSTGRES_CONNECT_TIMEOUT` if your server is simply slow to accept (a loaded host,
a distant region). The pool's own timeout is deliberately **not** shortened with it: that one
governs how long to wait for a *free* connection, and shortening it would start failing pools that
are merely busy.

A failed probe is remembered for `EXAMLOPS_POSTGRES_UNREACHABLE_TTL` seconds. Without that, the
budget is paid once per *connection* rather than once per command — invisible against a refused
port, where each probe is instant, but real against a black-holed host: `exa audit` opens the
datastore twice and cost 5.6 s with a 2 s budget, now 3.4 s. The verdict expires so a long-lived
process (dashboard, bridge) recovers by itself when the server comes back.

Two cases are left to libpq rather than guessed at — **unix-socket DSNs** (no TCP port to probe)
and **multi-host DSNs** (failover is the point of listing several hosts). Both skip the probe.

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
- **A new column must be added the way the schema adds one.** Column migrations
  (`platform_db._COLUMN_MIGRATIONS`) are `ALTER TABLE … ADD COLUMN` statements, and they go through
  the same translation — so declare the SQLite type the rest of the schema uses (`DATETIME`, `REAL`,
  `INTEGER`) and let the layer map it. Writing a Postgres type directly breaks SQLite instead. This
  path was *not* translated until a migration declared `DATETIME`, which Postgres does not have, and
  every process that opened the datastore died at startup; the unit suite now translates every entry
  in that table, so a SQLite-ism cannot reach a running platform again.
- **The audit log's tamper-evidence survives.** The append-only guards on `audit_events` become
  Postgres triggers, and the hash chain's cross-process write lock becomes
  `pg_advisory_xact_lock`. `exa audit verify` behaves identically on both engines.

### Writing SQL that has to run on both engines

Two rules, both learned from defects that reached a running platform.

**A `LIKE` pattern must be bound, never written into the statement.** psycopg parses the statement
for placeholders whenever parameters are passed, so a literal `%` in it is read as a broken one:

```
psycopg.errors.ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%''
```

```python
# wrong — works on SQLite, raises on Postgres
conn.execute("SELECT … WHERE job LIKE 'train:%' AND mlflow_run_id=?", (run_id,))
# right — the pattern is a value on both engines
conn.execute("SELECT … WHERE mlflow_run_id=? AND job LIKE ?", (run_id, "train:%"))
```

Doubling it to `%%` is not the alternative: that is a literal `%%` to SQLite, and the platform runs
on both. Two live defects were found this way, silent because their callers treat bookkeeping as
best-effort — **no cost or carbon lineage was ever recorded on a Postgres install**, and the agent's
platform-ops error listing raised.

`tests/unit/test_sql_has_no_literal_percent.py` fails the build on a new one. It reads the **AST**
rather than lines, which matters because the defect has more than one spelling:

| Written as | Why a line reader misses it |
|---|---|
| `"… job LIKE 'train:%' AND id=?"` | — this one it always caught |
| `"… job LIKE "` / `"'train:%' AND id=?"` | the formatter splits long SQL at this repo's 100-character limit; Python merges the literals, a line reader does not |
| `f"… job LIKE '{pattern}'"` | the `%` arrives by interpolation, and the value is unbound as well |
| `"… job ILIKE 'train:%'"` | Postgres' case-insensitive spelling |

Each shape has its own test, because a guard is only as good as the spellings it can see — the
middle two were invisible until the scanner was probed with them. The single exemption is the SQLite
backup tier, which reads `sqlite_master` in its own file and never meets psycopg.

**A new column is added the way the schema adds one** — see the bullet above; declare the SQLite
type and let the translation layer map it.

## Migrating existing state

There is no automatic SQLite→Postgres data copy yet. For a fresh deployment, point at Postgres from
the start. For an existing one, treat it as a migration project: export what you need
(`exa audit export`, the FinOps cost tables) and re-import. The audit hash chain cannot be
transplanted row-by-row without re-chaining, so plan a checkpoint (`exa audit checkpoint`) before
the cut-over and keep the SQLite file as the archival record of everything before it.

## When the datastore goes away

Every platform process bounds its use of the datastore, so an outage costs milliseconds per call
rather than tying up the service. The chaos drill
(`tests/integration/test_datastore_outage_drill_live.py`, `EXAMLOPS_CHAOS_LIVE=1`) kills Postgres
under a real control plane and a real gateway authorization service and measures it.

| What | How it is bounded |
|---|---|
| Opening a connection | `connect_timeout` from `EXAMLOPS_POSTGRES_CONNECT_TIMEOUT` (2 s) |
| A query on a connection whose server vanished | libpq keepalives and `tcp_user_timeout` (`EXAMLOPS_POSTGRES_TCP_TIMEOUT_MS`, 10 s). Without it the kernel retransmits for minutes: there is no reset to notice |
| Waiting for a free pooled connection | `EXAMLOPS_POSTGRES_POOL_TIMEOUT` (2 s), instead of the driver's 30 s |
| Every call after the first failure | An "unreachable" verdict cached for `EXAMLOPS_POSTGRES_UNREACHABLE_TTL` (5 s): those calls fail in microseconds without touching the network |
| Reconnecting when the server returns | The failed pool is discarded, so the next call connects at once rather than waiting out the driver's exponential backoff |

A DSN that names any of these libpq parameters keeps its own value, so a site can tune or disable
each one.

**What the drill measured** (Postgres killed with SIGKILL, then started again):

| Moment | Before this bounding | Now |
|---|---|---|
| `/readyz` answers 503 after the store dies | never: it hung past 40 s, so probes timed out and the control plane stopped answering anything | 2.0 s, and the calls after it are instant |
| A retrain submitted during the outage | — | `503`, naming the store; nothing half-written is left |
| A virtual key the gateway had verified | — | still allowed, from its cache ([static stability](serving-gateway.md#when-a-dependency-is-down)) |
| A key it had never seen | — | `503`, never waved through |
| The control plane is ready again after Postgres accepts connections | 16 s | 0.03 s, with no restart |

Postgres' own crash recovery dominates the total, and it varies with the machine and how much the
server had to replay: 1.8 s to 24 s to accept connections again across these runs, with the platform
following within a moment of that either way. Size a probe for the slow end, not the fast one. Readiness probes are set to a 3 s
timeout in the Helm chart for this reason, and liveness deliberately uses `/livez`, which touches
nothing — a datastore outage must never restart the replicas that would otherwise recover.

## Current limits

Honest status, so you can decide whether this fits your deployment:

- Verified: **the whole unit suite runs on Postgres 16** — 6323 passed, 38 skipped
  (`make test-postgres`), alongside the dedicated live-backend suite
  (`tests/integration/test_postgres_backend_live.py`, 13 tests) covering schema creation, the audit
  hash chain, append-only enforcement, upserts, timestamps, `lastrowid`, and pooled concurrent
  writers.
- **There are no Postgres-only failures left** (2026-09-12): the same run on SQLite fails the same
  tests and no others. Getting there closed two product defects this engine had and SQLite did not
  — the [column-migration translation](#how-it-works) and the [literal `%`](#writing-sql-that-has-to-run-on-both-engines)
  — and a set of tests that could not pose their question here and so were not asking one.
- The skips are honest, not hidden: the SQLite-backup-tier and schema-once-bootstrap tests (both
  keyed to a `platform.db` **file**), each naming its reason. (The dashboard-reader test that used
  to skip here now runs on both engines — it is the guard on the connection change below.)
- **Backups cover the platform datastore on this engine** — see [Backing it up](#backing-it-up)
  below. The 10 skipped backup tests are the SQLite *file*-snapshot tests, which have nothing to
  snapshot here; the Postgres path has its own 18 tests
  (`tests/unit/test_backup_platform_datastore.py`).
- **The dashboard reads the configured engine** — `dbconn.connect()` ignores the SQLite path it is
  handed and opens Postgres when `EXAMLOPS_DB_BACKEND=postgres`, so the consoles and the CLI can no
  longer end up on different stores. It needs `platform/cli/src` on `PYTHONPATH` (the container sets
  this); if the import fails it keeps serving on SQLite and logs an error saying it is not reading
  platform state, rather than failing to start.
- The dashboard suite runs against both configured engines: **665 tests, passing on Postgres with
  pooling enabled and on SQLite alike** (2026-09-13; the three that fail on both are an unrelated
  moto/aiobotocore drift). Shared fixture helpers isolate Postgres rows between
  tests, and connection-scope guards prevent new pooled-connection leaks.
- `exa data retention-prune --vacuum` and `exa doctor`'s DB checks are SQLite-specific.

Progress and remaining public work are summarized in this guide and the linked architecture records.

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
  .venv/bin/pytest tests/unit/ -q -n auto

EXAMLOPS_POSTGRES_TEST_DSN='postgresql://examlops:examlops@localhost:15433/examlops' \
  .venv/bin/pytest tests/integration/test_postgres_backend_live.py -v
```

The live-backend suite **drops and recreates the `public` schema**, which is why it reads a
dedicated `EXAMLOPS_POSTGRES_TEST_DSN` and never the DSN your platform runs on. Under
`EXAMLOPS_DB_BACKEND=postgres` the unit suite truncates between tests (`tests/conftest.py`), so
point it at a throwaway database too.

`-n auto` is safe here: each worker takes its own schema (`exa_test` → `exa_test_gw0`, `…_gw1`, …)
before it opens a connection, so the per-test truncation cannot reach another worker's rows. The
schemas are reused between runs rather than dropped — they are emptied per test anyway, and one per
worker is a bounded set.

One thing the command above does **not** get from the stock image is connection headroom. `-n auto`
is as many workers as you have cores, each with a pool, each sibling schema with another, and every
test that shells out to `exa` opens one more in its subprocess; on a 24-core machine that passes
`postgres:16-alpine`'s default `max_connections=100` and the run fills with
`FATAL: sorry, too many clients already` — a harness limit that reads like a platform fault. The
worker pools are capped at 2 connections for this reason, and `make test-postgres` starts its
container with `-c max_connections=300`. Add the same flag to the `docker run` above, or cap `JOBS`.

See
[the testing guide](testing.md#running-the-suite-on-postgres) for the two helpers to use when a
test's *precondition* (an absent table, a pre-migration table) is not portable between engines.
