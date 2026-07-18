# Backup, Restore & Disaster Recovery

> Enterprise-readiness Phase 0, item 0.9. The `platform.db` monolith is the data layer, event
> bus, and security store for 5+ processes — losing it, or restoring it inconsistently, is the
> single biggest blast-radius event on the platform. This runbook makes recovery a tested,
> repeatable procedure with defined RPO/RTO.

## What gets backed up

| Tier | Tool | Owned by |
|---|---|---|
| **`platform.db`** (audit, drift, cost, projects, approvals, …) | `exa backup` (SQLite online backup) | this runbook |
| MLflow / Postgres metadata | `pg_dump` | §Postgres below |
| MinIO artifacts (models, datasets) | `mc mirror` | §MinIO below |

The `exa backup` command owns the SQLite tier — the one the `SqliteBackend` (item 0.1) runs by
default and the hardest to recover consistently. Postgres/MinIO have first-class native tools and
are documented here for completeness.

## `platform.db` — `exa backup`

The backup is an **online, transactionally-consistent snapshot** via SQLite's native backup API —
safe to take while the CLI, agent, bridge, and control plane are actively writing. A plain
`cp platform.db` of a live WAL database can capture a torn state; **do not** use it.

```bash
# Create a snapshot (default ./backups) — writes platform-<UTC>.db + a .manifest.json
exa backup create --out /srv/examlops/backups

# List available snapshots (newest first)
exa backup list --dir /srv/examlops/backups

# Verify a snapshot BEFORE trusting it: checksum vs manifest + SQLite integrity + audit hash-chain
exa backup verify /srv/examlops/backups/platform-20260718T120000Z.db

# Restore (guarded): refuses to clobber a non-empty DB unless --force; re-verifies after
exa backup restore /srv/examlops/backups/platform-20260718T120000Z.db --force
```

Each snapshot ships a manifest recording its `sha256`, per-table row counts, the audit-chain head
hash, size, and timestamp. `verify` recomputes the audit hash chain against the snapshot, so a
tampered or corrupted backup is rejected **before** it can overwrite a live database. `restore`
re-runs that verification on the restored file, so a bad restore fails loudly instead of silently
leaving a broken DB.

### Scheduling

Run `exa backup create` from cron / a systemd timer and prune old snapshots:

```cron
0 * * * *  exa backup create --out /srv/examlops/backups   # hourly
0 3 * * *  find /srv/examlops/backups -name 'platform-*.db' -mtime +14 -delete
```

## The DR drill (tested restore)

The restore path is exercised in CI so it can never rot:

```bash
make dr-drill      # backup → wipe the live DB → restore → assert full recovery + valid audit chain
```

`tests/unit/test_backup_restore.py` performs the full create → destroy → restore round trip and
asserts every row returns and the tamper-evident audit chain still verifies. Treat a red
`dr-drill` as a release blocker.

## SLOs

| Objective | Target | How it's met |
|---|---|---|
| **RPO** (max data loss) | ≤ 1 h | hourly `exa backup create` |
| **RTO** (time to restore) | ≤ 5 min | single `exa backup restore` (seconds for a typical DB) |

Tune the cron cadence to lower RPO; RTO scales with DB size (online backup + verify).

## Postgres (MLflow metadata)

```bash
# Backup
docker compose exec -T postgres pg_dump -U mlflow mlflow | gzip > mlflow-$(date -u +%Y%m%dT%H%M%SZ).sql.gz
# Restore (into a fresh DB)
gunzip -c mlflow-<ts>.sql.gz | docker compose exec -T postgres psql -U mlflow mlflow
```

## MinIO (artifacts)

```bash
# Mirror the buckets to an offsite target (configure `backup` alias with `mc alias set`)
mc mirror --overwrite local/mlflow backup/mlflow
mc mirror --overwrite local/examlops-projects backup/examlops-projects
# Restore
mc mirror --overwrite backup/mlflow local/mlflow
```

## Recovery order

1. **MinIO** (artifacts must exist before the registry references resolve).
2. **Postgres** (MLflow metadata — model versions, runs).
3. **`platform.db`** (`exa backup restore`) — audit/drift/cost/projects.
4. `exa doctor` + `exa status` to confirm the platform is coherent, then `exa audit verify` to
   confirm the audit chain restored intact.
