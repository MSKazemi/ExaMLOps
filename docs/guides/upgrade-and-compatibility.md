# Upgrades & compatibility

Installing a new ExaMLOps release replaces the **core**. It must then open data that an older
release wrote: models, projects, pipelines, the audit chain, and the site's configuration and
pack. This page explains how a release decides whether it can open that data, how the data is
brought forward, and how to go back.

## A probe names a route the image has to serve

The chart's readiness and liveness probes are part of its contract with the image it deploys. When
the serving gateway's readiness probe moved from `/healthz` to `/readyz` on 2026-09-14, an older
control-plane image — which serves `/healthz` but not `/readyz` — produced pods that answered `404`
to every probe, never became ready, and left `helm upgrade --wait` to expire with nothing more
informative than `context deadline exceeded`.

So: **deploy a chart with the image it shipped with.** Mixing a newer chart with an older image is
not a partial upgrade, it is a rollout that cannot converge. The release pipeline ships them
together for this reason, and `make helm-kind-live` builds the image from the current tree before
testing the chart rather than trusting whatever is on the machine.

## The data-format stamp

Every platform datastore (SQLite `platform.db` or the Postgres schema) holds a small stamp in
its `platform_meta` table:

| Field | Meaning |
|---|---|
| `instance_id` | A UUID created the first time the data is opened. It identifies the *data*, so a backup records which instance it came from. |
| `data_format` | The highest migration applied to the data |
| `min_reader_format` | The lowest release data format that may still open it. Only a **breaking** migration raises it. |
| `created_with` / `last_opened_with` | The ExaMLOps versions that created the data and last opened it |

The rule works like SQLite's own read/write version bytes. Suppose a release works with data
format **C** and the data is stamped with data format **F** and minimum reader **R**:

| Condition | Verdict | What happens |
|---|---|---|
| no stamp yet | `unstamped` | The release stamps the data the first time it opens it. Data that already holds records is *adopted* at the baseline format, so every pre-stamp install upgrades cleanly. |
| C = F | `current` | Nothing to do |
| C > F, all pending steps online | `upgrade_available` | The steps apply automatically when any process opens the data |
| C > F, an offline step pending | `upgrade_required` | Services keep running (the release still tolerates the old shape). Run `exa upgrade apply`. |
| R ≤ C < F | `newer_compatible` | An older release on data a newer one wrote, and still able to read it. This is a **rollback** inside the compatibility window. |
| C < R | `too_new` | **Refused.** The platform raises `IncompatibleDataError` rather than misread the data. |

## Migrations

The schema still evolves *additively*: `CREATE TABLE IF NOT EXISTS` plus ADD-only column
migrations, which a guard test enforces. Additive changes need no step and never break a
rollback. Anything else is a **migration**, an ordered and append-only entry in
`examlops.lifecycle.migrations`. Examples are a backfill, a re-keying or a value rewrite.

- **Online** migrations are idempotent and safe to run while other replicas are serving. A new
  release applies them the first time any process opens the datastore, so an ordinary upgrade
  needs no operator step.
- **Breaking** migrations make the data unreadable to older releases, which is the contract step
  of expand/contract. They are never online. They run only through `exa upgrade apply`, which
  takes a verified backup first. They raise `min_reader_format`, and that is what makes an older
  release refuse the data instead of corrupting it.

Every step is recorded in `platform_upgrades`, with the backup bundle it can be undone from, and
as an audit event:

```bash
exa upgrade history
```

## Upgrading, step by step

```bash
# 1. The undo, before anything moves
exa backup create --all --push

# 2. Install the new release (pick one)
git pull && make install-dev                                    # source checkout (exact uv.lock versions)
docker compose pull && exa stack up                             # Compose
helm upgrade examlops platform/infra/helm/examlops \
  --set global.imageRegistry=registry.example.org/ -f site-values.yaml   # Kubernetes

# 3. The new release's verdict on the data
exa upgrade plan

# 4. Back up and run the pending migrations
exa upgrade apply            # --dry-run to preview; --tier … to widen the pre-upgrade backup

# 5. Pre-flight the whole install
exa instance check           # exit 1 on any problem — use it as a CI or maintenance-window gate
```

### The pre-upgrade backup has to be a rollback point

`exa upgrade apply` takes a bundle **before** it migrates, and refuses to go on unless that bundle
actually captured something. Until 2026-09-14 it refused only `overall_status: failed` — and a
bundle whose every requested tier was **skipped** reports `skipped`, not `failed`. That is a
directory with a manifest and no data in it, so the migration ran against production data with no
rollback point at all.

Both `failed` and `skipped` now stop the upgrade, and the refusal says what to fix:

```
⚠ Pre-upgrade backup: ./backups/examlops-backup-<ts> (skipped)
✗ refusing to upgrade: the pre-upgrade backup is 'skipped' — it captured nothing, so there would
  be no rollback point. Check the backup tiers (`exa backup create --all` and
  `exa backup verify-bundle`), or re-run with `--no-backup` if you have a rollback point of your own.
```

`partial` is allowed through **only if the bundle holds the tier this instance keeps its platform
state in** — `postgres` under `EXAMLOPS_DB_BACKEND=postgres`, `sqlite` otherwise. That distinction
matters because "at least one tier was captured" is not the same as "the data being migrated was
captured":

- The default tier list is now chosen from the engine. It was `["sqlite", "config"]` on *every*
  engine, and under Postgres the sqlite tier skips the platform DB **on purpose** (its state is in
  Postgres, dumped by the `postgres` tier, which that default never asked for). The bundle reported
  `partial` because the config tier succeeded — so on the enterprise backend the pre-upgrade backup
  held none of the data the migration was about to change.
- Asking for the right tier is not the same as getting it. If the `postgres` tier is requested and
  captures nothing — `pg_dump` missing, the server unreachable — the upgrade refuses on what the
  manifest **holds**, not on what was requested.

A config tier with nothing to snapshot still lets the upgrade run, which is the case that must not
be blocked. Widen what is captured with `--tier`, and note the status line is no longer a green tick
when the bundle is empty.

### If a migration fails

The question worth answering before step 4 runs against production data: a migration dies on row
10,000 — a bad row, a lost connection, a killed pod — **is the datastore now half old and half
new?**

No. Each migration runs inside its own transaction together with the stamp update that records it,
so a migration that raises leaves:

- **no partial data** — everything it wrote is rolled back;
- **the stamp where it was** — the format is not advanced, and a *breaking* step that died does not
  raise the minimum-reader floor, so older releases are not locked out of data that was never
  actually migrated;
- **the migration still pending** — `exa upgrade plan` lists it again, and running `exa upgrade
  apply` retries it from the start;
- **a non-zero exit and the error**, not a success line.

This holds on both engines and is pinned by
`tests/unit/test_lifecycle_dataformat.py::test_a_migration_that_dies_partway_leaves_nothing_behind`,
which runs a migration that writes a row and then raises, on SQLite and — through
`make test-postgres` — on Postgres. The test is paired with a migration that *succeeds*, so it
cannot pass over a mechanism that is simply unable to write.

So the recovery is to fix the cause and run `exa upgrade apply` again. The pre-upgrade bundle from
step 4 is there for the failure this cannot cover: a migration that completes and turns out to have
been *wrong*.

**Read what `apply` reports, not just the exit code.** `applied` lists what this process migrated
and can legitimately be empty — another replica may have won the race and done it first. What says
the instance is finished is `ok`, which is derived from the data afterwards, and `pending_after`,
which names anything still outstanding. On the terminal, an empty `applied` prints as *none
pending*; if migrations did not apply, you get an error naming them instead.

Beyond the datastore, the plan and the check also look at two other things:

- **Site profile.** An unknown module or preset in the site profile is a warning. The profile
  may name a module that a newer release renamed.
- **Use-case pack.** A pack may declare which releases it supports:

  ```toml
  # usecase/pack.toml
  [pack]
  name = "my-centre"
  requires_examlops = ">=0.52,<1.0"
  ```

  `exa instance check` fails when the running release is outside that range.

### On Kubernetes

Pods open the datastore at start, so online migrations apply on the first rollout of a new
release. For a release with offline migrations, enable the chart's hook. It runs
`exa upgrade apply` as a `pre-install,pre-upgrade` Job with the control-plane image, before any
new pod starts. A failed migration then fails the `helm upgrade` rather than leaving new pods on
data they cannot read:

```yaml
upgrade:
  hook:
    enabled: true
```

The Job does not back up Postgres. Take a CloudNativePG backup (or a `pg_dump`) first.
`helm rollback` stays safe after any upgrade whose migrations were not breaking.

## Going back

| Situation | Do this |
|---|---|
| Rolled back after an ordinary (additive or online) upgrade | Nothing. The older release sees `newer_compatible` and runs. |
| Rolled back after a **breaking** migration | The older release refuses the data (`too_new`). Restore the pre-upgrade bundle that `exa upgrade apply` recorded, which `exa upgrade history` names, then start the older release. |
| You understand the risk and must open it anyway | `EXAMLOPS_ALLOW_INCOMPATIBLE_DATA=1` for that process only |

## Backups and restores respect the stamp

A backup bundle's `bundle.manifest.json` records the `instance_id`, `data_format` and
`min_reader_format` of the data it captured. `exa backup restore-bundle` checks that stamp before
anything is touched. It **refuses** a bundle that this release could not read. A bundle from
before the stamp existed is treated as the baseline format and restores normally; the next open
brings it forward.

A bundle's config tier also carries the data root's own content: `site.toml`, the site's
`usecase/` pack, `config/` and `.providers/`. It captures the site configuration directory and the
site profile as well when those live elsewhere. So a restore onto a fresh install brings back the
centre's models, pipelines and module selection, not only its databases.

## Related

- [Core · deployment · instance data](three-layer-architecture.md)
- [Backup, restore & disaster recovery](backup-restore.md)
- [Production hardening](production-hardening.md) (rolling upgrades, the additive-migration rule)
