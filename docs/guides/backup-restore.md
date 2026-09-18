# Backup, Restore & Disaster Recovery

> Enterprise-readiness Phase 0, item 0.9 (extended to whole-platform DR). The platform is many data
> stores — the `platform.db` monolith (data layer + event bus + security store), the control-plane
> and agent SQLite DBs, the operator's on-disk config, the MLflow + Prefect Postgres metadata, and
> the MinIO object buckets. Losing any of them, or restoring them inconsistently, is a blast-radius
> event. This runbook makes recovery a tested, repeatable procedure with defined RPO/RTO.

## The model: one tiered bundle

`exa backup` produces a **bundle** — a single directory capturing every tier, each with a verifiable
manifest, aggregated into a top-level `bundle.manifest.json` with an overall status. Heavy tiers are
**opt-in** and **degrade gracefully**: if a tool or endpoint is missing the tier is recorded as
`skipped` (not failed) and the bundle still succeeds. The control-plane profile (SQLite + config)
has **zero external dependencies** and always works.

| Tier | What | Tooling | Default? |
|---|---|---|---|
| **sqlite** | `platform.db` (audit, drift, cost, projects…) + `approvals.db` + agent/skipper DBs | online SQLite backup (built-in) | ✅ always |
| **config** | `~/.config/examlops/` (config.toml, clusters.yaml, policy.yaml, finops.yaml, providers/); secrets **key-ids only** | tar (built-in) | ✅ always |
| **postgres** | MLflow + Prefect metadata DBs — **plus the platform datastore itself** when `EXAMLOPS_DB_BACKEND=postgres` | `pg_dump -Fc` (needs `postgresql-client`) | `--with-postgres` / `--all` |
| **objects** | MinIO buckets `mlflow-artifacts` + `examlops-projects` | boto3 S3 mirror (`examlops[backup]`) | `--with-objects` / `--all` |
| **content** | use-case packs, `pipelines/envs/*.yaml`, the `.dualgit/` classification | tar (built-in) | `--with-content` / `--all` |

> **Rotating the KEK is gated by the exit code — check it.** `exa secrets rewrap` re-encrypts every
> secret under the new active key so the old one can be decommissioned. It **exits 1 if any secret
> could not be rewrapped**, in `--json` mode as well as interactively (that was true only of the
> interactive path until 2026-09-14). Retiring the previous key after a failed rewrap makes every
> secret still wrapped under it permanently unreadable, so:
>
> ```bash
> exa --json secrets rewrap || { echo "rotation incomplete — do NOT retire the old key"; exit 1; }
> ```
>
> The payload's `failed` count and `errors` list name the secrets that were left behind.

> **Secrets / KEK caveat.** The secrets *ciphertext* lives in `platform.db` (captured by the sqlite
> tier), but it is useless without the KEK, which is held in the `EXAMLOPS_SECRETS_KEYS` env — never
> on disk. The config tier records the **key-ids** present and emits a warning; it never writes the
> plaintext KEK into the bundle. **Back up `EXAMLOPS_SECRETS_KEYS` out-of-band** (a secret manager),
> or a restored platform cannot decrypt its secrets.

> **Which tier holds platform state depends on the engine.** On the default SQLite engine it is the
> sqlite tier. Under `EXAMLOPS_DB_BACKEND=postgres` the `platform.db` file is an empty leftover —
> the postgres tier dumps the real store (scoped to `EXAMLOPS_POSTGRES_SCHEMA` when set) and the
> sqlite tier records `platform` as `skipped` with that reason. Bare `exa backup create` refuses on
> that engine rather than writing a hollow bundle; use `--with-postgres` or `--all`. See
> [the Postgres backend guide](postgres-backend.md#backing-it-up).

## Creating backups

```bash
# Fast control-plane bundle (all SQLite DBs + config) into ./backups
exa backup create

# Everything — + Postgres + MinIO buckets + use-case content, then replicate off-site
exa backup create --all --push

# Selective heavy tiers
exa backup create --with-postgres --with-objects

# Fail (don't skip) any requested tier that can't run — use in CI / a strict DR drill
exa backup create --all --strict

# A single classic platform.db snapshot (no tier flags → the original behaviour)
exa backup create --out /srv/examlops/backups
```

Every SQLite snapshot is an **online, transactionally-consistent** copy via SQLite's native backup
API — safe to take while the CLI, agent, bridge, and control plane are writing. A plain
`cp platform.db` of a live WAL database can capture a torn state; **do not** use it.

### Read the status line, not the tick

A tier that cannot run is **skipped**, not fatal — that is the point of the degrade convention, and
it is why the command reports what it actually did:

| `overall_status` | What you see | Exit code |
|---|---|---|
| `ok` | green `✓ Bundle written` | 0 |
| `partial` / `skipped` | yellow `⚠ Bundle written but incomplete`, then every tier that produced nothing, with its reason | 0 |
| `failed` | red `✗ Bundle incomplete`, with the tier that broke | **1** |

The incomplete and failed lines go to **stderr**, so `--quiet` cannot hide them. This matters most
for the object tier: if `MLFLOW_S3_ENDPOINT_URL` is not exported, the tier cannot reach MinIO, and a
bundle with no model artifacts in it is exactly the kind of backup you only discover at restore
time. Use `--strict` in CI and in a DR drill to turn any unrunnable tier into a non-zero exit at
the point it happens.

## Verifying & restoring

```bash
# Verify a whole bundle: manifest + every tier item's checksum + platform.db audit hash-chain
exa backup verify-bundle ./backups/examlops-backup-<ts>

# Restore the SQLite + config tiers (the default) — guarded; verifies BEFORE touching anything
exa backup restore-bundle ./backups/examlops-backup-<ts>

# Restore everything incl. Postgres + objects (DANGEROUS — drops/overwrites; requires --force)
exa backup restore-bundle ./backups/examlops-backup-<ts> \
    --tier sqlite --tier config --tier postgres --tier objects --force

# Classic single-DB restore (unchanged): refuses to clobber a non-empty DB unless --force
exa backup restore ./backups/platform-<ts>.db --force
```

**Compatibility (ADR 0128).** Every bundle records the data-format stamp of the data it captured
(`instance_id`, `data_format`, `min_reader_format`). `restore-bundle` refuses a bundle the running
release could not read — install that release first, or pick an older bundle — and a bundle from
before the stamp existed restores as the baseline format and is brought forward on the next open.
When `EXAMLOPS_DATA_DIR` is set, the config tier also carries the data root's own content —
`site.toml`, the site's `usecase/` pack, `config/`, `.providers/` — and restores it in place, so a
fresh install gets back the centre's models, pipelines and module selection, not just its
databases. See [Upgrades & compatibility](upgrade-and-compatibility.md).

`restore-bundle` verifies the bundle first and refuses an unverified one. Restores auto-create a
**rollback bundle** first (a fast control-plane backup, best-effort). The SQLite tier re-verifies
integrity + the audit chain after restoring, so a bad restore fails loudly.

### A partial restore says so

**The default restores `sqlite` and `config` only.** A bundle taken with `--all` also holds
`postgres` and `objects` — the models and the MLflow artifacts — and those are left behind unless
named with `--tier`. Until 2026-09-14 the command's last line was a green
`✓ Restored tiers ['config', 'sqlite']` and said nothing about the rest, which after a disaster
reads as *the platform is back*. The confirmation prompt named the tiers, but a scripted recovery
passes `--yes` and never sees it.

It now says what is still missing, before the tick, with the command that finishes the job:

```
⚠ NOT restored: objects, postgres — these tiers are in the bundle but outside the default
  (sqlite, config). The platform is only partly back. Restore them with:
  exa backup restore-bundle ./backups/examlops-backup-<ts> --tier config --tier objects
  --tier postgres --tier sqlite
✓ Restored tiers ['config', 'sqlite'] from ./backups/examlops-backup-<ts>.
```

### `ok` and `complete` answer different questions

A disaster-recovery script asks the second one, and until 2026-09-14 only the first existed:

| Field | Means | A deliberate `--tier sqlite` restore |
|---|---|---|
| `ok` | everything I was **asked** to restore came back | **true** |
| `complete` | everything the **bundle held** came back | **false** |

`ok` has to stay true for a partial restore, or every deliberate one would read as a failure — so
it cannot be the flag that says "the platform is fully back". `complete` is. The exit code follows
`ok`, deliberately and unchanged, because a script that asked for one tier and got it did not fail.

```bash
exa --json backup restore-bundle ./backups/<bundle> --yes | jq -e '.complete' \
  || echo "the bundle held more than was restored — see .skipped_tiers"
```

`restore_bundle()` returns `available_tiers`, `skipped_tiers` and `complete` alongside
`restored_tiers`, so a script never has to parse output. **The warning is printed in `--json` mode
too**, on stderr — stdout still carries exactly one document, and a human watching a scripted
recovery still sees that the platform is only partly back.

**A tier counts as left behind only if the bundle captured something for it.** `postgres` and
`objects` appear in *every* manifest with `status: skipped` and zero items when the stack was not
up at backup time — there is nothing there to restore, and naming them on every restore is the
noise that gets a warning ignored. That distinction is asserted in both directions in
`tests/unit/test_backup_bundle.py`.

## Scheduling & off-site replication

A dedicated Compose **backup sidecar** runs `exa backup schedule` inside the stack (reaching Postgres
and MinIO), creating a bundle each interval, pruning per retention, and pushing off-site:

```bash
# Opt-in profile; enable off-site by setting EXAMLOPS_BACKUP_S3_URI first
EXAMLOPS_BACKUP_S3_URI=s3://examlops-backups/nightly \
  docker compose --profile backup up -d backup
docker compose logs -f backup
```

!!! warning "Kubernetes installs have no backup workload yet"

    The Helm chart deploys the control plane, dashboard and agent, and nothing else — there is no
    backup `CronJob`. It also sets `EXAMLOPS_DB_BACKEND=postgres`, so the platform state a bundle
    would need lives in Postgres, not in a file any pod carries. Until the chart ships one, schedule
    backups **outside** the cluster: run `exa backup create --all --push` from a host that can reach
    the Postgres service and the object store, and set `EXAMLOPS_BACKUP_PG_DBS` to include the
    platform database. Point `EXAMLOPS_DB_BACKEND=postgres` at that host too, or the sqlite tier
    will happily archive an empty local `platform.db` and the bundle will look complete.

For non-Docker installs, run it from a **host systemd timer** (or cron):

```ini
# /etc/systemd/system/examlops-backup.service   (Type=oneshot)
ExecStart=/usr/local/bin/exa backup create --all --push
# A failed off-site push exits non-zero, so wire the unit that tells you:
OnFailure=examlops-backup-failed.service
# /etc/systemd/system/examlops-backup.timer
[Timer]
OnCalendar=daily
```

Manual off-site management:

```bash
exa backup list --remote                       # list off-site bundles
exa backup pull examlops-backup-<ts> --dest ./restore   # fetch one (then verify-bundle)
exa backup prune --dir ./backups --keep 14     # rotate: keep newest 14 (never the last good one)
exa backup status                              # latest bundle, per-tier health, off-site reachability
```

### Create the off-site bucket yourself, and check that the push worked

**Nothing creates the bucket for you.** `minio-init` provisions `mlflow-artifacts`, project storage,
the dataplane bucket and the dashboard's docs bucket — there is deliberately no backups bucket,
because an "off-site" copy on the same MinIO as the data it protects is not off-site. Point
`EXAMLOPS_BACKUP_S3_URI` at a store that survives losing this host, and create the bucket there
before enabling the sidecar.

If you do not, every push fails with `NoSuchBucket` and the **local backup still succeeds**, which
is the right behaviour — a broken off-site target must never cost you a good local bundle. What was
wrong was that it was the *only* behaviour: the failure went to a log line, the cycle returned the
local bundle's status, and the operator's last line was `✓ One cycle complete`. An instance whose
replication had never once worked looked exactly like one replicating every hour, and stayed that
way until the host was gone. A cycle now reports the outcome:

```console
$ exa backup schedule --once --tiers sqlite --push
✓ One cycle complete: examlops-backup-20260913T161539Z (status=partial).
✗ Off-site replication FAILED — this bundle exists only on this host. NoSuchBucket: …
$ echo $?
1
```

On success it names where the copy went, and `--json` carries the same under an `offsite` key. The
local bundle's `status` is untouched either way: it describes the bundle, not the copy.

!!! warning "Nothing alerts you when backups stop working"

    There is no `BackupFailed` alert, because the sidecar is a CLI loop with no metrics endpoint to
    scrape — so a backup that has been failing for a month is not going to tell you. Until that
    changes, watch one of the two signals the platform does produce:

    - every cycle writes a `backup_schedule_run` audit event carrying `status`, `pushed` and
      `push_error` — `exa audit --last 7d` will show a run of `pushed: false`;
    - **both** `exa backup create --push` and `exa backup schedule --once --push` exit non-zero
      when a requested replication failed, which is enough for a systemd timer's `OnFailure=`, a
      cron mail, or a CI maintenance job. The local bundle is still written and still verifiable —
      the non-zero exit is about the copy that would survive losing this host.

    `exa backup status` reports the latest bundle and off-site reachability on demand.

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `EXAMLOPS_BACKUP_DIR` | `./backups` | local bundle root |
| `EXAMLOPS_BACKUP_TIERS` | `sqlite,config` | scheduled profile |
| `EXAMLOPS_BACKUP_INTERVAL` | `3600` | scheduler seconds |
| `EXAMLOPS_BACKUP_RETAIN` | `keep=14` | retention (`keep=N` and/or `days=D`) |
| `EXAMLOPS_BACKUP_S3_URI` | *(off)* | off-site target `s3://bucket/prefix` |
| `EXAMLOPS_BACKUP_S3_ENDPOINT` / `_ACCESS_KEY` / `_SECRET` | MinIO fallbacks | off-site creds (override for real AWS) |
| `EXAMLOPS_BACKUP_PG_DBS` | `mlflow,prefect` | Postgres DBs to dump |
| `EXAMLOPS_BACKUP_BUCKETS` | `mlflow-artifacts,<projects>` | object buckets to mirror |
| `EXAMLOPS_BACKUP_ON_PROMOTE` | `false` | auto-backup before an autopilot promotion |

A `[backup]` section in `config.toml` can set the same keys (env wins).

## Auto-backup before risky operations

A fast control-plane bundle is taken automatically (best-effort, never blocking) before:

- `exa backup restore` / `exa backup restore-bundle` — a rollback point;
- `exa secrets rewrap` — before re-encrypting under a new KEK;
- an autopilot promotion (when `EXAMLOPS_BACKUP_ON_PROMOTE` is enabled).

## The DR drill (tested restore)

```bash
make dr-drill   # backup → wipe → restore → assert full recovery + valid audit chain
```

`tests/unit/test_backup_restore.py` (single-DB) and `tests/unit/test_backup_bundle.py`,
`test_backup_tiers.py`, `test_backup_ops.py` (tiered bundle, retention, scheduler, off-site) perform
full round trips with **no live stack** — heavy tiers degrade or use in-memory fakes. Treat a red
`dr-drill` as a release blocker.

### Can you actually restore it?

Read the paragraph above carefully, because for a long time it said more than it did. Those round
trips run on **SQLite**. On the Postgres engine, platform state lives in a `pg_dump` archive that
`pg_restore` puts back — a different tier, a different binary, a different failure surface — and
everything covering it was unit-level: the argv is right, the password never reaches `ps`, the dump
is scoped to the configured schema, a failure is reported rather than swallowed. All true, and none
of it says the data comes back. **The enterprise restore path had never been run end to end.**

It is now, by a drill that uses a real server:

```bash
EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_postgres_dr_roundtrip_live.py -v -s
```

It starts and removes its own Postgres (or uses `EXAMLOPS_POSTGRES_DR_DSN`), seeds a chained audit
log and a traffic split, takes a `postgres`-tier bundle, **drops the schema**, restores, and then
checks more than that rows exist: the chain's **head hash must equal the one from before**. A
restore that rewrote the log while preserving its contents would pass every weaker check and quietly
destroy the only property the audit log is for. It needs `pg_dump`/`pg_restore` on `PATH` and skips
without them — a DR drill that silently did not run is worse than no DR drill — and it runs weekly
with the rest of [the drill set](game-days.md).

The first run earned its place: it found that `restore_bundle` cleared the cached "this schema is
already initialised" verdict after the *sqlite* tier and not after the *postgres* tier. Exactly one
of the two holds platform state at a time, so under the Postgres engine the clearing ran on the tier
that was empty and was skipped on the tier that had just been replaced, leaving the process
convinced a schema it had never looked at was ready — and skipping the additive DDL, the
data-format stamp and every online migration the restored data might need.

The **objects** tier had the same hole, and it mattered more because this guide puts it *first* in
the recovery order below:

```bash
EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_objects_dr_roundtrip_live.py -v -s
```

That drill starts its own MinIO, writes model artifacts, backs them up, **deletes the bucket**, and
restores — then compares each object's **digest**, because a restore that put back the right number
of empty or truncated files passes anything that counts objects and surfaces much later as a model
that will not load. It found that restoring into a store which had lost its bucket raised a raw
`NoSuchBucket`: the backup knew the bucket had existed, and the restore would not recreate it. The
unit suite had missed it because its in-memory S3 double created buckets on upload — a double more
forgiving than the real thing, on the one path that only matters in a real disaster. The restore now
recreates a missing bucket and reports each bucket's outcome separately, so one bad bucket cannot
cost you the report on the others.

### Does the container behind the RPO run?

The objective below says RPO ≤ 1 h, and what meets it is a container — the Compose `backup`
sidecar. The release workflow builds and signs that image, Compose wires it, and unit guards check
its YAML. **Nothing ever started it**, and an objective whose mechanism has never run is a wish: a
missing extra, an entrypoint that will not take its command, a directory it cannot write are all
invisible until the day you need the backup.

```bash
EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_backup_sidecar_live.py -v -s
```

That drill builds the shipped image, runs **one real cycle**, and then reads what the container
wrote rather than trusting its exit code: the manifest must show the platform datastore captured
(a bundle of nothing but skipped tiers exits 0 just as happily), the bundle must verify against the
host's own checksums, and the database inside it must still carry its audit chain.

It found this: **the bundles the sidecar writes could not be verified by the operator who reads
them.** Verification opened the snapshot with the platform's ordinary hardened connection, which
sets `journal_mode=WAL` — a write. The sidecar runs as root inside its container, so its bundles
belong to root, and an operator verifying one as themselves got *attempt to write a readonly
database* instead of a verdict. The same would happen anywhere backups are supposed to live: a
read-only mount, WORM storage, an archive restored with its ownership intact. Verification now
opens the snapshot read-only and immutable, which is what it should always have done — **a
verifier must leave what it verifies exactly as it found it.**

Worth knowing if you reproduce it: the bug only bites a snapshot on a *rollback* journal, because
`PRAGMA journal_mode=WAL` against a file that is already WAL writes nothing. A test that happens to
use a WAL snapshot passes over it completely.

## SLOs

| Objective | Target | How it's met |
|---|---|---|
| **RPO** (max data loss) | ≤ 1 h | hourly scheduled bundle (sidecar), [exercised by a drill](#does-the-container-behind-the-rpo-run) |
| **RTO** (time to restore) | ≤ 5 min | `exa backup restore-bundle` (seconds for a typical control-plane DB) |

Tune the interval to lower RPO; RTO scales with data size (Postgres/object tiers dominate a full DR).

## Recovery order (full disaster)

1. **objects** (MinIO artifacts must exist before the registry references resolve).
2. **postgres** (MLflow/Prefect metadata — model versions, runs; on the Postgres engine this is
   also where `platform.db`'s content comes back from).
3. **sqlite** (`platform.db` — audit/drift/cost/projects; agent + approvals DBs). On the Postgres
   engine the platform entry is absent here by design — step 2 restored it.
4. **config** + restore `EXAMLOPS_SECRETS_KEYS` **out-of-band** so encrypted secrets decrypt.
5. `exa doctor` + `exa status` to confirm coherence, then `exa audit verify` for the audit
   chain. **`exa doctor` exits 1 when it finds anything**, so this step can be scripted —
   until 2026-09-14 it printed its findings and exited 0, and a scripted recovery check
   therefore passed whatever it found.

`exa backup restore-bundle <dir> --tier objects --tier postgres --tier sqlite --tier config --force`
performs 1–4 in one command.
