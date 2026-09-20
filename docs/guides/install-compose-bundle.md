# Install on a single node (Compose bundle)

The Compose install bundle runs ExaMLOps on one Linux machine from the released container images.
You don't clone the repository and nothing is built on your machine. Use it for a first
production node, a pilot site, or an evaluation VM. For a cluster, use the Helm chart (see
[Enterprise installation](enterprise-installation.md)).

The bundle lives in `platform/infra/docker-compose/install/`. Each release will also publish it
as a tarball asset, with the matching version already filled in.

## What you get

| Service | Image | Default address |
|---|---|---|
| Dashboard | `examlops-dashboard` | `http://localhost:18099` |
| MLflow (registry and tracking) | `examlops-mlflow` | `http://localhost:15000` |
| Prefect (orchestration) | upstream `prefecthq/prefect`, pinned by digest | `http://localhost:14200` |
| Ray Serve (inference) | `examlops-ray-serving` | `http://localhost:18001` |
| Control plane (retrain API) | `examlops-control-plane` | `http://localhost:18002` |
| Skipper agent (dashboard Copilot) | `examlops-agent` | loopback only |
| PostgreSQL | `examlops-postgres` | internal |
| MinIO (optional; or your own S3 service) | upstream MinIO, pinned by digest | `:19000` API, `:19001` console |
| Backup sidecar (opt-in) | `examlops-backup` | — |
| Monitoring (opt-in): Prometheus, Alertmanager, Grafana, Loki, Tempo | upstream, pinned by digest | Grafana `:13000`, Prometheus `:19090` |

Every ExaMLOps image is pulled from `ghcr.io/mskazemi` at the bundle's version. Every upstream
image is pinned by digest, so two installs of the same release run the same bytes.

The bundle doesn't include JupyterHub, the Dataplane bus bridge or vLLM. They still run from the
development stack (`platform/infra/docker-compose/docker-compose.yml`).

## Requirements

- Linux with Docker Engine and the Compose v2 plugin. On Ubuntu 22.04 or 24.04, install
  `docker-ce` and `docker-compose-plugin` from Docker's apt repository, or Ubuntu's `docker.io`
  and `docker-compose-v2` packages. The bundle was checked with Docker 29.7 and Compose 5.5 on
  Ubuntu 24.04.
- About 12 GB of free memory for the core services (Ray Serve alone is capped at 6 GB) and a few
  GB of disk for images.
- Outbound access to `ghcr.io` and Docker Hub, or a registry mirror (see
  [Air-gapped sites](#air-gapped-sites)).

## Install

```bash
cd examlops-compose            # the unpacked release asset, or platform/infra/docker-compose/install
./install.sh init              # from a checkout, add: --version X.Y.Z
```

`init` writes `.env` with mode `600`, fills every credential with a fresh random value, and
creates `./state`. It refuses to overwrite an existing `.env`.

Before the first start, review these values in `.env`:

| Variable | Why |
|---|---|
| `PUBLIC_HOST` | The hostname users type. Used for dashboard links and MLflow's CORS. |
| `EXAMLOPS_BIND` | `127.0.0.1` by default. Every service speaks plain HTTP, so set `0.0.0.0` only behind a firewall or a TLS reverse proxy. |
| `CONTROL_PLANE_ALLOWED_HOSTS` | Add `PUBLIC_HOST` when the control plane is reached by name. |
| `AGENT_CONTAINER_OLLAMA_URL` | An Ollama server for the Skipper agent. Everything else works without one. |
| `EXAMLOPS_SEED_PACK` | See [Add your models](#add-your-models). |

Then start the stack:

```bash
./install.sh check             # every placeholder filled, compose file valid
docker compose up -d
docker compose ps              # wait until every service is healthy
```

Sign in to the dashboard as `admin`, with the `DASHBOARD_ADMIN_PASSWORD` value from `.env`.

If a required credential is missing, Compose refuses to start and names the variable. The
bundle has no fallback credentials such as `minioadmin`.

## Where your data lives

Everything the platform creates lives in three places. Back them up together:

- `./state` is the instance-data root from ADR 0128, mounted at `/state` in every service. It
  holds `platform.db` (audit chain, projects, drift, lineage), `site.toml`, `usecase/`,
  `config/`, `.providers/` and `agent/` (the Skipper agent's memory). The one-shot
  `instance-init` service creates this layout on every start and never overwrites what is
  already there. It also gives the directory to the uid and gid `install.sh init` recorded
  (`EXAMLOPS_STATE_UID`/`EXAMLOPS_STATE_GID`). That lets you edit it without `sudo`, and the
  agent, which runs as uid 10001, can write to it through the group.
- The `postgres_data` volume holds MLflow and Prefect metadata.
- The object store holds model artifacts and project storage: the `minio_data` volume with the
  bundled MinIO, or your own S3 buckets.

The `backup` profile runs `exa backup schedule --all` inside the stack, hourly by default. It
captures `platform.db` and the agent's memory, the site files in `./state`, the dashboard's own
`exa` configuration, both Postgres databases, and the artifact and project buckets:

```bash
docker compose --profile backup up -d backup                         # scheduled
docker compose --profile backup run --rm backup backup create --all  # one now
```

Bundles are written to the `backups_data` volume. Set `EXAMLOPS_BACKUP_S3_URI` to copy each one
off the host. On a fresh install a few items report `skipped`, for example agent memory that
hasn't been created yet or MLflow's SQLite file, which the bundle doesn't use. That's expected.

## Use your own S3 store

By default the bundle runs its own MinIO. To use an S3 service you already operate, such as
Ceph RGW, an institutional object store or a cloud bucket, edit `.env` before the first start:

1. Remove `minio` from `COMPOSE_PROFILES`, which leaves it empty. The bundled MinIO then never
   starts, and nothing else depends on it.
2. Set `EXAMLOPS_S3_ENDPOINT`, and `EXAMLOPS_S3_REGION` if your store uses one. Keep
   `EXAMLOPS_S3_ADDRESSING_STYLE=path` for MinIO, Ceph RGW and most on-premise stores; use
   `virtual` for AWS S3 buckets that need virtual-hosted addressing.
3. Set `EXAMLOPS_S3_ACCESS_KEY` and `EXAMLOPS_S3_SECRET_KEY` to an account that can create and
   write the three buckets.
4. Set `EXAMLOPS_S3_SERVING_ACCESS_KEY` and `EXAMLOPS_S3_SERVING_SECRET_KEY` to a key that can
   only read the artifact bucket. Ray Serve downloads models with it. The bundled MinIO issues
   this key itself; on your own store you issue it.
5. Bucket names are global on most public clouds, so give them a site prefix:
   `EXAMLOPS_S3_ARTIFACT_BUCKET`, `EXAMLOPS_PROJECTS_BUCKET` and `DASHBOARD_MINIO_BUCKET`.

The one-shot `s3-init` service creates any bucket that is missing on every start, whichever
store you use. It runs in the platform's own backup image, so an install on your own store
pulls no MinIO image at all. If a bucket name belongs to another account, or the key lacks
access, it stops the start with a message naming the setting to change. MLflow, the dashboard, Ray Serve and the backup sidecar all reach the store
through `EXAMLOPS_S3_ENDPOINT`.

## Turn on monitoring

Add `monitoring` to `COMPOSE_PROFILES` in `.env` (for example `COMPOSE_PROFILES=minio,monitoring`)
and run `docker compose up -d`. You get:

- **Prometheus**, which scrapes Ray Serve, the control plane and the monitoring services, plus
  any cluster nodes and HPC vLLM endpoints that `exa hpc prometheus-sd` writes to
  `./state/prometheus-targets`.
- **The platform's alert rules and Alertmanager.** The `Watchdog` alert fires constantly by
  design, so a dead-man's switch can tell that alerting still works.
- **Grafana** with the platform dashboards, at `:13000`. Sign in as `admin` with
  `GRAFANA_ADMIN_PASSWORD` from `.env`.
- **Loki and Promtail**, which collect every container's logs through a read-only Docker API
  proxy. Promtail never mounts the Docker socket itself.
- **Tempo** for traces. To send traces, also set `OTEL_SDK_DISABLED=false` and
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317`.

Alertmanager's receivers read their secrets from files in `./secrets/alertmanager/`, which
`install.sh init` creates with mode `0750`: `slack_api_url`, `pagerduty_routing_key` and
`heartbeat_url`, one value per file. A missing file turns that receiver off. Give each file mode
`0640`: Alertmanager reads them through your group, so they're never world-readable. This
directory is never part of a release tarball.

## Add your models

ExaMLOps doesn't ship with any models of its own. They come from a use-case pack (ADR 0094), and
every service reads the same one, from `./state/usecase`. With no pack, the platform starts empty.

- To start from a pack shipped inside the images, set `EXAMLOPS_SEED_PACK` before the first
  start, for example `EXAMLOPS_SEED_PACK=/app/usecases/reference`. `instance-init` copies it into
  `./state/usecase` once.
- To use your own pack, copy it into `./state/usecase`, with its `pack.toml` at the top, and
  restart with `docker compose up -d`.

## Expose it

Keep `EXAMLOPS_BIND=127.0.0.1` and put a TLS-terminating reverse proxy on the host, for example
Caddy or nginx. Point it at `127.0.0.1:18099` (dashboard) and any other service users need.
Leave the Ray dashboard and the agent on loopback. The Ray dashboard's Jobs API runs arbitrary
code without authentication, so the bundle always binds it to `127.0.0.1`.

## Upgrade

The host needs no `exa` installation. The commands run inside the `instance-init` container,
which carries `exa` and sees `./state`:

```bash
docker compose --profile backup run --rm backup backup create --all   # 1. the undo
# 2. unpack the new release's bundle over this directory (.env, state/ and secrets/ stay), then:
./install.sh upgrade-env                                              #    new settings in, version moved
docker compose pull
docker compose run --rm --entrypoint exa instance-init upgrade plan   # 3. what the release makes of the data
docker compose stop                                                   # 4. only if the plan asks for it:
docker compose run --rm --entrypoint exa instance-init upgrade apply  #    backs up, then migrates offline
docker compose up -d                                                  # 5. start the new release
docker compose run --rm --entrypoint exa instance-init instance check # 6. exit 1 on any problem
```

`upgrade-env` adds the settings a new release introduces and generates any new secrets. It
never changes a value that's already set. A setting that was renamed keeps its old value, so
credentials that a volume already holds (MinIO's root account, for example) aren't rotated.
Credentials, `./state` and the volumes carry over. Read the release notes first for any step
that release requires. [Upgrades and compatibility](upgrade-and-compatibility.md) explains what a
release promises to keep.

## Air-gapped sites

Mirror the release into a registry you control, then install from it. Two details decide whether
the result can be trusted and pulled:

- Copy the ExaMLOps images with `oras cp -r`, which brings their signatures and provenance along.
  A plain image copy leaves the signatures behind. Then set `EXAMLOPS_REGISTRY` to the mirror.
- The upstream images keep their own names, so `EXAMLOPS_REGISTRY` doesn't reach them. Mirror
  them under the same path and digest, and point Docker's `registry-mirrors` at the mirror. That
  covers the Docker Hub images. The two MinIO images come from quay.io, so they also need a
  `docker-compose.override.yml`.

[Air-gapped and mirrored installs](air-gapped-install.md) has the tested commands, and the offline
verification to run before `docker compose up`.

## Known limits

- **Bundles v0.54.0 to v0.56.0 cannot pull MinIO any more.** MinIO removed `minio/minio` and
  `minio/mc` from Docker Hub on 2026-09-11. Those bundles name them there, so on a host that has
  not pulled them before, `docker compose pull` stops with `pull access denied for minio/minio`.
  This affects every install, not only the `minio` profile, because the `s3-init` job uses
  `minio/mc`. The same images, with the same digests, are on quay.io. Put this
  `docker-compose.override.yml` next to `docker-compose.yml`; Compose merges it automatically:

    ```yaml
    services:
      minio:
        image: quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e
      minio-init:
        image: quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727
      s3-init:
        image: quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727
    ```

  Later bundles name quay.io themselves.
- **MinIO:** MinIO stopped publishing community container images after `RELEASE.2025-09-07`,
  which is the release the bundle pins. That image receives no security fixes. For production,
  [use your own S3 store](#use-your-own-s3-store). If you keep the bundled MinIO, keep it on
  loopback (the default) behind a firewall.
- **Datastore:** the platform datastore defaults to SQLite in `./state/platform.db`, which suits
  one node. For Postgres, set `EXAMLOPS_DB_BACKEND=postgres` and `EXAMLOPS_POSTGRES_DSN` (see
  [Postgres backend](postgres-backend.md)).
- **TLS:** the bundle doesn't terminate TLS itself.

## How the bundle is kept honest

`tests/unit/test_install_bundle.py` reads the bundle on every CI run. It fails if a service
builds an image, mounts anything besides `./state` (or the Docker socket, read-only, in the
socket proxy), references an ExaMLOps image the release workflow doesn't publish, uses an
upstream image without a digest, or reads a credential that isn't required and generated. It
also fails if a weak default reappears, a port ignores `EXAMLOPS_BIND`, a service names the
bundled MinIO or a bucket directly, or `env.template` falls out of step with the compose file.
