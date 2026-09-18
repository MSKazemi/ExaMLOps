# Production Hardening Checklist

> Enterprise-readiness Phase 0, items 0.6 + 0.8. The committed `docker-compose.yml` is a **dev**
> topology (see roadmap 1.1 — the enterprise topology is K8s/Helm). This checklist is what a
> production overlay MUST set. Items marked ✅ are already enforced in code/compose or gated by a
> test (`tests/unit/test_compose_security.py`).

## Secrets — no insecure defaults

| Variable | Dev default | Production requirement |
|---|---|---|
| `CONTROL_PLANE_TOKEN` | *(empty → writes 503)* ✅ | Strong random token; the app rejects `changeme`/placeholders (fail-closed, QW6). It grants every action, so once each service has its own credential retire it: `CONTROL_PLANE_LEGACY_TOKEN=warn`, then `off` ([how](../components/control-plane.md#retiring-the-shared-legacy-token)). Better than a per-service secret is a short-lived SPIFFE identity ([workload identities](../components/control-plane.md#workload-identities-spiffe)). |
| `DASHBOARD_JWT_SECRET` | *(required `:?`)* ✅ | ≥32-char random; compose refuses to start unset. |
| `DASHBOARD_SECRET_KEY` | *(required `:?`)* ✅ | Fernet key; compose refuses to start unset. |
| `DASHBOARD_ADMIN_PASSWORD` / `DASHBOARD_VIEWER_PASSWORD` | *(required `:?`)* ✅ | Strong, unique. |
| `GRAFANA_ADMIN_PASSWORD` | `admin` | **Override** with a strong password. |
| `GRAFANA_ANONYMOUS_ENABLED` | `true` | Set **`false`** unless you deliberately expose read-only dashboards. |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` | **Override** both; rotate regularly. After the per-service keys below are set, only `minio` and `minio-init` hold these. |
| `MINIO_{SERVING,MLFLOW,DASHBOARD,BACKUP,NOTEBOOK}_{ACCESS,SECRET}_KEY` | *(unset → that service falls back to root)* ✅ | **Set all five pairs.** `minio-init` creates each user with only what its service needs. Serving and backup are read-only; MLflow is read-write on `mlflow-artifacts`; the dashboard is read-write on its docs bucket and project storage. Notebooks, which run arbitrary user code, get read-write on artifacts and project storage and never root. None of them can create buckets or users. Guarded by `tests/unit/test_compose_storage_credentials.py`. |
| `POSTGRES_PASSWORD` | `mlops` | **Override.** After the per-service roles below are set, only `postgres`, `postgres-init` and the backup sidecar hold it. |
| `{MLFLOW,PREFECT,DASHBOARD}_DB_{USER,PASSWORD}` | *(unset → that service logs in as the superuser)* ✅ | **Set all three pairs**, plus `DASHBOARD_DB_NAME=dashboard`. Each service then owns its own database and cannot connect to another's. See [Each service has its own Postgres role](#each-service-has-its-own-postgres-role-plan-p34). |
| `EXAMLOPS_SIGNING_KEY` | *(unset → sign refuses)* ✅ | Real key or a D7 secret; `exa audit checkpoint` fails closed without one (item 0.7). |

## Docker socket — least privilege ✅

The dashboard controls services (start/stop/restart, log tail) via Docker. It talks to the
**`docker-socket-proxy`** (`tecnativa/docker-socket-proxy`), never the raw socket:

- The proxy holds `/var/run/docker.sock` **read-only** and exposes only `CONTAINERS` + `POST`.
- `EXEC`, `IMAGES`, `VOLUMES`, `NETWORKS`, `SWARM`, `SECRETS`, `AUTH`, `SYSTEM`, … are all **denied**.
- The dashboard reaches it via `DOCKER_HOST=tcp://docker-socket-proxy:2375` — `docker.from_env()`
  honors it, so no code change was needed.

A raw `/var/run/docker.sock:rw` mount is root-equivalent host takeover; the gate test fails the
build if it reappears on the dashboard.

## Network & host scoping

- **Allowed hosts** ✅ — the control plane honors `CONTROL_PLANE_ALLOWED_HOSTS` (comma-separated).
  Default `*` (dev); set a real allow-list in prod to reject Host-header spoofing.
- **CORS** — no service enables a permissive/wildcard `CORSMiddleware`; cross-origin is closed by
  default. Keep it that way; front the platform with a reverse proxy / ingress that terminates TLS.
- **TLS** — terminate TLS at the ingress/reverse proxy for every published port. None of the dev
  containers speak TLS directly.
- **Published ports** — in prod, publish only the ingress. The dev compose publishes every UI port
  to `localhost` for convenience; do not do that on a shared host.

### Segmented container networks (plan P3.5) ✅

The base Compose file puts every container on one network, so code running in any container (a
notebook, above all) can connect to Postgres, the Docker API proxy or any internal port. The
`docker-compose.segmented.yml` overlay replaces that flat network with zones, and each service
joins only the zones of the services it talks to:

```bash
docker compose -f docker-compose.yml -f docker-compose.segmented.yml up -d
# or in .env: COMPOSE_FILE=docker-compose.yml:docker-compose.segmented.yml
```

| Zone | Members |
|---|---|
| `db` *(internal)* | Postgres and the services that keep state or metadata in it |
| `objects` *(internal)* | MinIO and the services that move artifacts |
| `control` | control plane, Prefect, MLflow, dashboard, agent, NATS, the bridge, the event consumers |
| `ops` | Prometheus, Alertmanager, Grafana, Loki, Promtail, Tempo, and what they scrape or receive from |
| `notebooks` | JupyterHub, every notebook it spawns, and exactly MLflow, MinIO, Prefect, Ray Serve and the control plane |
| `docker-api` *(internal)* | the Docker socket proxy and the dashboard, its only client |

What it guarantees, checked by `tests/unit/test_compose_segmentation.py`:

- **Nothing is cut.** The test derives about 60 service-to-service dependencies from the Compose
  environment, the `.env` template, `depends_on`, the Prometheus, Grafana and Promtail configs,
  JupyterHub's notebook environment, and the runtime-only ones (the NATS backbone, the Postgres
  backend). It fails if any pair shares no network, or if a service is missing from the overlay.
- **A notebook reaches only what a notebook needs.** Never Postgres, the Docker API proxy, NATS,
  the dashboard, the agent or the backup sidecar.
- **Only the dashboard reaches the Docker API**, and Postgres only its clients.
- **`db`, `objects` and `docker-api` are internal**, so no traffic leaves the host through them.

Published ports are unaffected: segmentation governs traffic between containers. Kubernetes
deployments get the same zoning from the Helm chart's NetworkPolicies (`networkPolicy.enabled`).

### MLflow and Prefect require authentication (plan P3.6) ✅

MLflow and Prefect are open to anything on their network by default. Anything that can reach them
can register a model, move an alias or start a flow run. Both can require a credential, and
every platform caller sends it once configured. In `.env`:

```bash
# MLflow: its basic-auth app, with an admin account you choose
MLFLOW_AUTH=basic
MLFLOW_ADMIN_PASSWORD=<random>              # required; MLflow's bundled default is public
MLFLOW_FLASK_SERVER_SECRET_KEY=<random>
MLFLOW_TRACKING_USERNAME=admin              # what the platform's services send
MLFLOW_TRACKING_PASSWORD=<the admin password, or a service user's>

# Prefect: one auth string, used by the server and every client
PREFECT_API_AUTH_STRING=<user>:<random>
```

- **Who sends it.** MLflow's and Prefect's own SDKs (Ray Serve, the training flows, notebooks)
  read these variables themselves. The platform's raw-HTTP callers send the same header through
  `examlops.service_auth`: the control plane's Prefect gateway, dispatch probe and
  serving-snapshot compiler; the dashboard's registry, pipeline and embedded-UI proxy (still
  gated by dashboard roles); the agent's tools; and the `exa` CLI. The credential is sent only to
  URLs under the configured MLflow or Prefect address, and never replaces an `Authorization` a
  caller already set.
- **Where else to set it.** Also set the same variables wherever a Prefect worker runs flows
  outside Compose, for example a systemd runner on the host. Notebook users get their own MLflow
  accounts, which the admin creates in the MLflow UI, rather than the service credential.
- **Health probes stay open.** MLflow's `/health` and Prefect's `/api/health` answer without
  credentials, so container healthchecks and `/readyz` keep working.
- **Verified live.** With authentication on, an anonymous call and MLflow's bundled default
  password both get 401, `examlops.service_auth`'s headers get 200, and the serving snapshot
  compiles through it. With it off, both servers behave exactly as before.

### Services prove who they are with short-lived identities (ADR 0125) ✅

A static secret lives until someone changes it, and a copy is as good as the service. Run the
Compose identity overlay and each service calls the control plane with a five-minute JWT-SVID
that SPIRE issues to its container by label and renews on its own:

```bash
docker compose -f docker-compose.yml -f docker-compose.identity.yml up -d
```

Static secrets keep working as a fallback. Retire each one when
`control_plane_authentications_total{principal="<service>",method="static"}` stops growing.

The same overlay makes every call to the model server mutual TLS under the caller's identity and
closes its plaintext port: Ray Serve listens on loopback, `18001` is not published, and only the
control plane, the dashboard and the agent may use its admin routes. Inference from outside the
platform goes through the serving gateway. Setup, verification and the security model:
[Workload identity](workload-identity.md).

### Prove it by breaking it: game days ✅

Every item on this page is a claim about how the platform behaves when something goes wrong.
[Game days](game-days.md) is how you check them: `make chaos-drills` kills the datastore, the event
backbone and a model server's capacity in throwaway containers and holds each one to what the
documentation promises, with the numbers to compare against. Two of the three drills found real
defects the unit suites could not see. The same guide covers running those failures against your own
staging installation.

### Each service has its own Postgres role (plan P3.4) ✅

MLflow, Prefect and the dashboard all log in to Postgres as the superuser by default, and the
dashboard keeps its tables inside MLflow's database. So any one of them, compromised, can read or
drop the others' data, including the model registry. Give each its own role in `.env`:

```bash
MLFLOW_DB_USER=mlflow         MLFLOW_DB_PASSWORD=<random>
PREFECT_DB_USER=prefect       PREFECT_DB_PASSWORD=<random>
DASHBOARD_DB_USER=dashboard   DASHBOARD_DB_PASSWORD=<random>   DASHBOARD_DB_NAME=dashboard
```

On `docker compose up`, the one-shot `postgres-init` service (`postgres-init/provision.sh`) runs as
the superuser before MLflow, Prefect and the dashboard start. For each service that has a role, it:

- creates the role, or updates its password: to rotate one, change the variable and run `up` again;
- makes the role the owner of the service's database, **including every table the superuser
  created before**, because MLflow's and Prefect's own schema migrations must own what they alter.
  Ownership moves object by object; `REASSIGN OWNED` would also hand over the other services'
  databases;
- takes away everyone else's right to connect to that database;
- gives the role no superuser, database-creation, role-creation or replication rights.

A service without a role keeps using the superuser, as before, so turning this on is per service
and a new install needs no extra step. `postgres-init` refuses two unsafe setups: a role without
a password, and a dashboard with its own role on MLflow's database (each would take the database
from the other).

**Moving an existing dashboard to its own database.** Its four tables and its migration record live
in MLflow's database. With the dashboard stopped and the variables above set:

```bash
cd platform/infra/docker-compose
docker compose stop dashboard
docker compose up postgres-init                        # creates the `dashboard` database
docker compose exec -T postgres sh -c 'pg_dump -U mlops -d mlflow --no-owner --no-privileges \
    -t dashboard_audit -t dashboard_config -t model_doc_images -t model_doc_overrides \
    -t dashboard_alembic_version | psql -v ON_ERROR_STOP=1 -U mlops -d dashboard'
docker compose up postgres-init                        # hands the copied tables to `dashboard`
docker compose up -d dashboard
```

Once the dashboard works, drop those five tables from the `mlflow` database. The backup sidecar
follows `DASHBOARD_DB_NAME`, so the new database is backed up with no change to
`EXAMLOPS_BACKUP_PG_DBS`.

`tests/integration/test_postgres_service_roles_live.py` (`EXAMLOPS_POSTGRES_ROLES_LIVE=1`) runs all
of this against the platform's Postgres image:

- On the real MLflow and Prefect schemas, first created by the superuser as on every existing
  install, MLflow and Prefect run their own migrations and write as their own roles. Without the
  ownership transfer they cannot.
- No role can connect to another's database, and a rotated password takes effect.
- The move above is replayed, and the dashboard then reads its rows as its own role.

The platform-state database (`EXAMLOPS_POSTGRES_DSN`, the Postgres backend) is separate: its
services still share one role.

## Filesystem

- The dev dashboard bind-mounts the whole repo `rw` so the host CLI and container share one
  `platform.db`. In prod (K8s, roadmap 1.1) this becomes a least-privilege PVC for the datastore
  only; the code is read from the image, not a live bind.

## Zero-downtime rolling upgrades (item 1.10)

The Helm chart's Deployments use `strategy: RollingUpdate` with **`maxUnavailable: 0`** + readiness
probes, so a new version only takes traffic once its pods are `Ready`, and old pods drain only after
the new ones are up — no capacity dip during an upgrade. Rollback is one command:

```bash
# --reuse-values keeps global.imageRegistry from the installed release; without it helm falls
# back to chart defaults, and the chart refuses to render with no registry set.
helm upgrade examlops platform/infra/helm/examlops --reuse-values --set controlPlane.image.tag=0.38.0
kubectl rollout status deploy/examlops-examlops-control-plane -n examlops   # gate on readiness
helm rollback examlops                                                       # instant, reversible
```

**Schema safety (expand/contract):** rolling upgrades only work if the new schema is still readable
by the *previous* app version. ExaMLOps migrations are **additive-only** — `CREATE TABLE IF NOT
EXISTS` + `ADD COLUMN` (nullable/defaulted), never `DROP`/`RENAME` in the same release — enforced by
`tests/unit/test_migrations_additive.py`. A destructive column is a two-release expand→contract, so
an old replica mid-rollout never hits a column it can't tolerate.

## Backups ✅

- Whole-platform backups via the Compose `backup` sidecar (`exa backup schedule`, off-site S3 +
  retention) or a host systemd timer; `make dr-drill` proves the restore path. See
  [backup-restore.md](backup-restore.md).

## The datastore grows, and retention covers two tables of fifty-two

`exa data retention-prune` prunes `drift_snapshots` and `input_snapshots`. The command says exactly
that, and its exclusions are deliberate — the audit chain and FinOps cost history are the two things
a retention job must never touch. **What it does not say is what happens to everything else.**

The schema has **52 append-only event tables**. Twenty are kept on purpose and should be: the audit
chain and its WORM anchors, supply-chain attestations, the EU-AI-Act register, lineage, evaluation
and gate reports, dataset and feature provenance, carbon and GPU cost history. Thirty more grow with
traffic and have **no retention decision recorded at all**. The fastest are per request:

| Table | Grows by |
|---|---|
| `gateway_calls` | one row per LLM request |
| `guardrail_events` | one per input/output scan — roughly two per request |
| `cache_events`, `routing_events`, `predictions` | one per request each |
| `ab_assignments`, `ab_results`, `shadow_results` | one per request under an A/B or shadow test |
| `agent_tool_calls` | one per agent step |

So on a busy gateway the platform datastore keeps growing whatever the retention job is set to, and
`exa data retention-prune`'s stated purpose — reclaiming space — is only partly served by it. Until
that changes, **watch the datastore's size as a first-class signal** rather than assuming retention
bounds it, and size the volume for the traffic you expect rather than for the pruned tables.

Expanding the prune list is a **retention-policy decision, not a code change**: the trade is data
minimisation against keeping evidence somebody may need, and adding a table starts deleting
operators' data on the next scheduled run. `tests/unit/test_retention_is_decided.py` makes the
decision visible instead of making it: every append-only table must be classified as retained (with
the reason) or as an open question, and a table added later belongs to neither until somebody says
which — so the next one cannot be forgotten the way these were.

## Verifying

```bash
make dr-drill                       # backup/restore round-trip
.venv/bin/pytest tests/unit/test_compose_security.py tests/unit/test_alerting_config.py -q
docker compose -f platform/infra/docker-compose/docker-compose.yml config --quiet
```
