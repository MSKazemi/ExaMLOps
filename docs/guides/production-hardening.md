# Production Hardening Checklist

> Enterprise-readiness Phase 0, items 0.6 + 0.8. The committed `docker-compose.yml` is a **dev**
> topology (see roadmap 1.1 — the enterprise topology is K8s/Helm). This checklist is what a
> production overlay MUST set. Items marked ✅ are already enforced in code/compose or gated by a
> test (`tests/unit/test_compose_security.py`).

## Secrets — no insecure defaults

| Variable | Dev default | Production requirement |
|---|---|---|
| `CONTROL_PLANE_TOKEN` | *(empty → writes 503)* ✅ | Strong random token; the app rejects `changeme`/placeholders (fail-closed, QW6). |
| `DASHBOARD_JWT_SECRET` | *(required `:?`)* ✅ | ≥32-char random; compose refuses to start unset. |
| `DASHBOARD_SECRET_KEY` | *(required `:?`)* ✅ | Fernet key; compose refuses to start unset. |
| `DASHBOARD_ADMIN_PASSWORD` / `DASHBOARD_VIEWER_PASSWORD` | *(required `:?`)* ✅ | Strong, unique. |
| `GRAFANA_ADMIN_PASSWORD` | `admin` | **Override** with a strong password. |
| `GRAFANA_ANONYMOUS_ENABLED` | `true` | Set **`false`** unless you deliberately expose read-only dashboards. |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` | **Override** both; rotate regularly. |
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

## Filesystem

- The dev dashboard bind-mounts the whole repo `rw` so the host CLI and container share one
  `platform.db`. In prod (K8s, roadmap 1.1) this becomes a least-privilege PVC for the datastore
  only; the code is read from the image, not a live bind.

## Zero-downtime rolling upgrades (item 1.10)

The Helm chart's Deployments use `strategy: RollingUpdate` with **`maxUnavailable: 0`** + readiness
probes, so a new version only takes traffic once its pods are `Ready`, and old pods drain only after
the new ones are up — no capacity dip during an upgrade. Rollback is one command:

```bash
helm upgrade examlops platform/infra/helm/examlops --set controlPlane.image.tag=0.38.0
kubectl rollout status deploy/examlops-examlops-control-plane -n examlops   # gate on readiness
helm rollback examlops                                                       # instant, reversible
```

**Schema safety (expand/contract):** rolling upgrades only work if the new schema is still readable
by the *previous* app version. ExaMLOps migrations are **additive-only** — `CREATE TABLE IF NOT
EXISTS` + `ADD COLUMN` (nullable/defaulted), never `DROP`/`RENAME` in the same release — enforced by
`tests/unit/test_migrations_additive.py`. A destructive column is a two-release expand→contract, so
an old replica mid-rollout never hits a column it can't tolerate.

## Backups ✅

- `exa backup create` on a schedule; `make dr-drill` proves the restore path. See
  [backup-restore.md](backup-restore.md).

## Verifying

```bash
make dr-drill                       # backup/restore round-trip
.venv/bin/pytest tests/unit/test_compose_security.py tests/unit/test_alerting_config.py -q
docker compose -f platform/infra/docker-compose/docker-compose.yml config --quiet
```
