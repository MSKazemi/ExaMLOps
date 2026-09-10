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
| PostgreSQL, MinIO | `examlops-postgres`, upstream MinIO | internal; MinIO on `:19000`/`:19001` |
| Backup sidecar (opt-in) | `examlops-backup` | — |

Every ExaMLOps image is pulled from `ghcr.io/mskazemi` at the bundle's version. Every upstream
image is pinned by digest, so two installs of the same release run the same bytes.

The bundle doesn't include the monitoring stack (Prometheus, Grafana, Loki, Tempo), JupyterHub,
the SeanerBUS bridge or vLLM. They still run from the development stack
(`platform/infra/docker-compose/docker-compose.yml`).

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
- The `minio_data` volume holds model artifacts and project storage.

The `backup` profile runs `exa backup schedule --all` inside the stack, hourly by default. It
captures `platform.db` and the agent's memory, the site files in `./state`, the dashboard's own
`exa` configuration, both Postgres databases and both MinIO buckets:

```bash
docker compose --profile backup up -d backup                         # scheduled
docker compose --profile backup run --rm backup backup create --all  # one now
```

Bundles are written to the `backups_data` volume. Set `EXAMLOPS_BACKUP_S3_URI` to copy each one
off the host. On a fresh install a few items report `skipped`, for example agent memory that
hasn't been created yet or MLflow's SQLite file, which the bundle doesn't use. That's expected.

## Add your models

ExaMLOps doesn't ship with any models of its own. They come from a use-case pack (ADR 0094), and
every service reads the same one, from `./state/usecase`. With no pack, the platform starts empty.

- To start from a pack shipped inside the images, set `EXAMLOPS_SEED_PACK` before the first
  start, for example `EXAMLOPS_SEED_PACK=/app/usecases/seanergy`. `instance-init` copies it into
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
# 2. set EXAMLOPS_VERSION in .env to the new release, then:
docker compose pull
docker compose run --rm --entrypoint exa instance-init upgrade plan   # 3. what the release makes of the data
docker compose stop                                                   # 4. only if the plan asks for it:
docker compose run --rm --entrypoint exa instance-init upgrade apply  #    backs up, then migrates offline
docker compose up -d                                                  # 5. start the new release
docker compose run --rm --entrypoint exa instance-init instance check # 6. exit 1 on any problem
```

Credentials, `./state` and the volumes carry over. Read the release notes first for any step
that release requires. [Upgrades and compatibility](upgrade-and-compatibility.md) explains what a
release promises to keep.

## Air-gapped sites

Copy the images into a registry you control (for example with `docker buildx imagetools create`
or `skopeo copy`) and set `EXAMLOPS_REGISTRY` to it. Upstream images are pinned by digest in
`docker-compose.yml`, so mirror them under the same digest.

## Known limits

- **MinIO:** MinIO stopped publishing community container images after `RELEASE.2025-09-07`,
  which is the release the bundle pins. That image receives no security fixes. The bundle can't
  yet point at an external S3 service instead, so the node must stay firewalled, with MinIO on
  loopback (the default).
- **Datastore:** the platform datastore defaults to SQLite in `./state/platform.db`, which suits
  one node. For Postgres, set `EXAMLOPS_DB_BACKEND=postgres` and `EXAMLOPS_POSTGRES_DSN` (see
  [Postgres backend](postgres-backend.md)).
- **TLS:** the bundle doesn't terminate TLS itself.

## How the bundle is kept honest

`tests/unit/test_install_bundle.py` reads the bundle on every CI run. It fails if a service
builds an image, mounts anything besides `./state` (or the Docker socket, read-only, in the
socket proxy), references an ExaMLOps image the release workflow doesn't publish, uses an
upstream image without a digest, or reads a credential that isn't required and generated. It
also fails if a weak default reappears, a port ignores `EXAMLOPS_BIND`, or `env.template` falls
out of step with the compose file.
