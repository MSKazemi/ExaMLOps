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
| **postgres** | MLflow + Prefect metadata DBs | `pg_dump -Fc` (needs `postgresql-client`) | `--with-postgres` / `--all` |
| **objects** | MinIO buckets `mlflow-artifacts` + `examlops-projects` | boto3 S3 mirror (`examlops[backup]`) | `--with-objects` / `--all` |
| **content** | use-case packs, `pipelines/envs/*.yaml`, the `.dualgit/` classification | tar (built-in) | `--with-content` / `--all` |

> **Secrets / KEK caveat.** The secrets *ciphertext* lives in `platform.db` (captured by the sqlite
> tier), but it is useless without the KEK, which is held in the `EXAMLOPS_SECRETS_KEYS` env — never
> on disk. The config tier records the **key-ids** present and emits a warning; it never writes the
> plaintext KEK into the bundle. **Back up `EXAMLOPS_SECRETS_KEYS` out-of-band** (a secret manager),
> or a restored platform cannot decrypt its secrets.

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

`restore-bundle` verifies the bundle first and refuses an unverified one. Restores auto-create a
**rollback bundle** first (a fast control-plane backup, best-effort). The SQLite tier re-verifies
integrity + the audit chain after restoring, so a bad restore fails loudly.

## Scheduling & off-site replication

A dedicated Compose **backup sidecar** runs `exa backup schedule` inside the stack (reaching Postgres
and MinIO), creating a bundle each interval, pruning per retention, and pushing off-site:

```bash
# Opt-in profile; enable off-site by setting EXAMLOPS_BACKUP_S3_URI first
EXAMLOPS_BACKUP_S3_URI=s3://examlops-backups/nightly \
  docker compose --profile backup up -d backup
docker compose logs -f backup
```

For non-Docker installs, run it from a **host systemd timer** (or cron):

```ini
# /etc/systemd/system/examlops-backup.service   (Type=oneshot)
ExecStart=/usr/local/bin/exa backup create --all --push
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

The restore path — single-DB **and** whole-platform bundle — is exercised in CI so it can never rot:

```bash
make dr-drill   # backup → wipe → restore → assert full recovery + valid audit chain (all tiers)
```

`tests/unit/test_backup_restore.py` (single-DB) and `tests/unit/test_backup_bundle.py`,
`test_backup_tiers.py`, `test_backup_ops.py` (tiered bundle, retention, scheduler, off-site) perform
full round trips with **no live stack** — heavy tiers degrade or use in-memory fakes. Treat a red
`dr-drill` as a release blocker.

## SLOs

| Objective | Target | How it's met |
|---|---|---|
| **RPO** (max data loss) | ≤ 1 h | hourly scheduled bundle (sidecar) |
| **RTO** (time to restore) | ≤ 5 min | `exa backup restore-bundle` (seconds for a typical control-plane DB) |

Tune the interval to lower RPO; RTO scales with data size (Postgres/object tiers dominate a full DR).

## Recovery order (full disaster)

1. **objects** (MinIO artifacts must exist before the registry references resolve).
2. **postgres** (MLflow/Prefect metadata — model versions, runs).
3. **sqlite** (`platform.db` — audit/drift/cost/projects; agent + approvals DBs).
4. **config** + restore `EXAMLOPS_SECRETS_KEYS` **out-of-band** so encrypted secrets decrypt.
5. `exa doctor` + `exa status` to confirm coherence, then `exa audit verify` for the audit chain.

`exa backup restore-bundle <dir> --tier objects --tier postgres --tier sqlite --tier config --force`
performs 1–4 in one command.
