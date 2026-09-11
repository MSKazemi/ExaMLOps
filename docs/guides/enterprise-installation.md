# Enterprise Installation & Configuration

How to install and configure ExaMLOps on a new machine, cluster, or environment — and an honest map
of what is production-ready today versus what a fully enterprise, one-command cluster install still
needs. Every path and knob below is grounded in the actual repo (Makefile targets, compose files, the
Helm chart, `.env.example`, and the `examlops.*` config seams).

## Maturity at a glance

| Install path | What it is | Status |
|---|---|---|
| **A — Single node (Docker Compose)** | `make bootstrap` → uv venv + `docker compose up` | **Production-in-use** (this is what runs on `lxp-cpu01`). Best path today for a new machine or single VM. |
| **B — Kubernetes (Helm)** | `helm install` control-plane + dashboard + agent | **Partial / reference.** Lints, renders, enterprise pod-security — but covers only 3 tiers, assumes you bring your own Postgres/MinIO/Redis, and every release publishes signed, scanned images and the chart to GHCR (`oci://ghcr.io/mskazemi/charts/examlops`, first: v0.54.0 — [Releases](release-process.md)). |
| **C — CI auto-deploy (GitLab → node)** | `deploy:lxp` SSHes to the node, `git pull`, rebuilds, smoke-gated auto-rollback | **Production-in-use, single-node.** Deploy-from-HEAD to one NFS host; no image registry, no canary. |

**Bottom line:** for a *new computer or single server* you can be fully running in ~15 minutes via
Path A. For a *brand-new multi-node enterprise cluster* the pieces exist as seams and a partial Helm
chart, but there is real gap-closing work (below) before it is a turnkey, HA, multi-tenant install.

## Three layers — what an install is made of

Whatever the path, an install is three layers ([Core · deployment · instance data](three-layer-architecture.md)):
the **core** (what a release replaces), the **deployment** (the Compose file or the Helm chart that
runs it) and the **instance data** your users create. Decide where the instance data lives *before*
the first user touches the platform — it is what every later upgrade must keep:

```bash
exa instance init --data-dir /srv/examlops-data --pack usecases/seanergy --preset standard
export EXAMLOPS_DATA_DIR=/srv/examlops-data      # set it for every process / service
exa instance info                                 # where every piece of user data lives
exa modules list                                  # which modules this centre runs
```

Then, per path: Compose mounts the state directory at `/state` and already sets
`EXAMLOPS_DATA_DIR=/state` (point `EXAMLOPS_STATE_DIR` at `/srv/examlops-data`); Helm keeps state in
the external Postgres + object store and takes the centre's modules from
`exa modules render --target helm`. Upgrading later is `exa upgrade plan` → `exa upgrade apply` →
`exa instance check` ([Upgrades & compatibility](upgrade-and-compatibility.md)); which modules run is
[a site profile](site-feature-profiles.md).

---

## Path A — Single node / small team (works end-to-end today)

The `make bootstrap` path. Suitable for a workstation, a single production VM, or an air-gapped box.

### Prerequisites
- Docker + Docker Compose v2
- [`uv`](https://docs.astral.sh/uv/) (the Makefile prompts to install it if missing)
- Python 3.12+ (`make` guards this)
- ~8 GB RAM for the full stack (Postgres, MLflow, Prefect, Ray, MinIO, dashboard, control plane)

### Steps
```bash
git clone <repo> examlops && cd examlops

# 1. Configuration — copy the template and set the REQUIRED secrets (backend fails fast without them)
cp .env.example .env
#   Generate the four required dashboard secrets:
python -c "import secrets; print('DASHBOARD_JWT_SECRET='+secrets.token_urlsafe(32))"     >> .env
python -c "from cryptography.fernet import Fernet; print('DASHBOARD_SECRET_KEY='+Fernet.generate_key().decode())" >> .env
#   Set DASHBOARD_VIEWER_PASSWORD and DASHBOARD_ADMIN_PASSWORD to strong values.
#   ⚠ DASHBOARD_SECRET_KEY encrypts secrets at rest — lose it and stored secrets are unrecoverable. Back it up.

# 2. One-shot bring-up: dev stack + install the `exa` CLI + all deps
make bootstrap
#   = touch-env-dashboard + stack-up (docker compose up -d --build) + install-dev (uv sync --frozen: uv.lock versions)

# 3. Verify
exa status
curl -s http://localhost:18099/api/health     # dashboard
```

### Optional profiles (add capabilities)
```bash
make monitoring-up     # Prometheus + Alertmanager + Tempo + Grafana + Loki + Promtail
make jupyter-up        # JupyterHub (per-project workbenches) on :18888
make control-plane-up  # retrain API on :18002 (needs CONTROL_PLANE_TOKEN)
make full-up           # stack + monitoring + SeanerBUS bridge (needs the seanerbus repo + network)
```

Service URLs (local): dashboard :18099 · MLflow :15000 · Prefect :14200 · Ray :18001/:18265 ·
control-plane :18002 · MinIO :19001 · Grafana :13000. Full list in the project README.

### Hardening a single-node prod box
Follow `docs/guides/production-hardening.md`: replace `minioadmin` defaults, put TLS in front (the
compose path is plaintext HTTP by default), scope `CONTROL_PLANE_ALLOWED_HOSTS`, keep the
docker-socket-proxy (least-privilege Docker API), enable the `backup` profile + run `make dr-drill`.

---

## Path B — Enterprise Kubernetes (the target shape)

The chart at `platform/infra/helm/examlops/` ships an enterprise **pod posture** — non-root
(uid 10001), read-only rootfs, drop-ALL caps, seccomp RuntimeDefault, PDBs, topology spread, and an
nginx Ingress with TLS. **It deploys only control-plane + dashboard + agent** and expects stateful
services externally. The control plane deliberately defaults to one replica: rate limits, retrain
locks, and poller ownership now use the configured coordinator, but the Prefect circuit breaker and
runtime ModelZoo configuration remain process-local and multi-replica failover is not yet verified.

### Prerequisites (bring-your-own managed services)
- A Kubernetes cluster (1.27+), an ingress controller, and cert-manager (or pre-provisioned TLS).
- **Postgres** (the chart references CloudNativePG — `postgres.host/readHost/database`).
- **Object storage** (MinIO or S3 — `minio.endpoint/bucket`).
- **Redis** for the implemented cross-host coordinator and Redis Streams event publisher. NATS and
  Kafka selectors remain fail-loud placeholders, not supported deployment backends.
- An external **Secret** holding the app secrets (never templated into the chart).

### Install
```bash
# 1. Create the app secret out-of-band (see the chart README for the full key list)
kubectl create secret generic examlops-secrets \
  --from-literal=DASHBOARD_JWT_SECRET=... \
  --from-literal=DASHBOARD_SECRET_KEY=... \
  --from-literal=DASHBOARD_ADMIN_PASSWORD=... \
  --from-literal=DASHBOARD_VIEWER_PASSWORD=... \
  --from-literal=CONTROL_PLANE_TOKEN=... \
  --from-literal=EXAMLOPS_POSTGRES_DSN='postgresql://...'

# 2. Validate then install
make helm-validate                       # lint + refusal check + render (+ kubectl dry-run if a cluster is reachable)
helm install examlops platform/infra/helm/examlops -f my-values.yaml \
  --set global.imageRegistry=<your-registry>/          # REQUIRED — note the trailing slash
```

`CONTROL_PLANE_TOKEN` is the legacy `legacy/default` operator credential. For tenant isolation,
store a `CONTROL_PLANE_CREDENTIALS_JSON` token map in the control-plane Secret and give the
dashboard only its corresponding bearer value as `CONTROL_PLANE_TOKEN`. Each map entry declares a
trusted `principal`, `tenant`, and `read`/`write` scopes; never place the JSON or token values in a
values file.

> **`global.imageRegistry` is required, and the chart refuses to render without it.** It used to
> default to empty, which composed references like `examlops-agent:0.48.0`. Kubernetes resolves an
> unqualified name to `docker.io/library/examlops-agent` — the Docker Official Images namespace,
> which only Docker can publish to — so the default asked for an image that can never exist, while
> `helm lint` still reported `0 chart(s) failed`. The chart now fails at render time with the flag
> to set, because that is the last point where the mistake is cheap.

> **Where the images come from.** A release publishes every image and the chart to GHCR — pushed by
> digest, scanned, cosign-signed and attested (see [Releases](release-process.md)), so
> `--set global.imageRegistry=ghcr.io/mskazemi/` pulls exactly what CI built. Before the first
> release, or to run from your own registry, build them: `make images` builds all three tiers at the
> platform version, and `IMAGE_PREFIX` tags them for your registry:
>
> ```bash
> make images IMAGE_PREFIX=ghcr.io/<owner>/     # control-plane · dashboard · agent
> docker push ghcr.io/<owner>/examlops-agent:<version>   # and the other two
> ```
>
> The agent image is new: the chart deployed that tier for a while with no Dockerfile behind it
> anywhere in the repository. It is built for the chart's pod posture — non-root uid 10001 and a
> read-only root filesystem — so every path Skipper writes to (`AGENT_DB`, `AGENT_MEMORY_DB`,
> `PLATFORM_DB`, `HOME`) defaults to `/tmp`, the one writable mount. Override them if you want
> persistence.

> **Every tier builds from the public repository.** The control plane's Dockerfile used to
> `COPY modelzoo`, which is upstream code the public repository does not carry, so a clean clone
> failed with `"/modelzoo": not found`. The model library is now an optional named build context:
> without it the image builds from the public tree alone (it still bakes in the default use-case
> pack's model registry, so `/health` lists the pack's models); a site that wants the library inside
> the image passes `--build-context modelzoo=<dir>`, which compose does from
> `EXAMLOPS_MODELZOO_BUILD_CONTEXT` (default: the checkout's `modelzoo/`).
> `tests/unit/test_dockerfile_build_context.py` fails if any tier COPYs a path the public tree lacks.

### Building a chart repository

`make helm-package` produces a complete, publishable Helm repository in `dist/helm/` — the chart
tarball plus an `index.yaml`. Publishing is then a copy of that directory to any static host:

```bash
make helm-package HELM_REPO_URL=https://<host>/<path>   # the URL is baked into index.yaml
# then, for consumers:
helm repo add examlops https://<host>/<path>
helm search repo examlops
helm install examlops examlops/examlops --set global.imageRegistry=<your-registry>/
```

The whole path — package → index → `helm repo add` → render through the repo — is verified offline
against a local HTTP server; only the hosting step is outstanding.

### Day-0 enterprise configuration (the env seams to turn on)
These are the flags that flip ExaMLOps from single-tenant dev to multi-tenant HA. They exist as
`examlops.*` seams with a working default and a loud-failing enterprise backend:

| Concern | Env / setting | Notes |
|---|---|---|
| **Data backend** | `EXAMLOPS_DB_BACKEND=postgres` + `EXAMLOPS_POSTGRES_DSN` | Working: all helpers reach it through `platform_db.get_db()`, which the backend now fronts (`examlops.storage.pg`), and the dashboard through `dbconn.connect()`. Verified live on Postgres 16 — schema, audit hash chain, append-only triggers — with the whole unit suite green against it. Connections are pooled (`EXAMLOPS_POSTGRES_POOL_MAX`, default 10 per process): see [Postgres backend](postgres-backend.md). |
| **Coordination** | `EXAMLOPS_COORDINATOR=redis` + `EXAMLOPS_REDIS_URL` | Implemented atomic cross-host leases, deduplication, and rate limits. `db` is the dependency-free default for processes sharing one datastore. |
| **Event backbone** | `EXAMLOPS_EVENT_PUBLISHER=redis` + `EXAMLOPS_REDIS_URL` | Implemented Redis Streams relay from the transactional outbox. Delivery is at least once with stable event IDs; consumers deduplicate. `log` is the dependency-free default. NATS/Kafka are placeholders. |
| **Identity / SSO** | `EXAMLOPS_OIDC_ISSUER` / `_AUDIENCE` / `_JWKS` (`examlops[oidc]`) | RS256 access-token validation. **Off by default** → dashboard passwords and static service bearer maps remain the trust roots. Turn this on for enterprise user identity. |
| **Multi-tenancy** | `EXAMLOPS_MULTITENANCY=1`; `CONTROL_PLANE_CREDENTIALS_JSON` | Platform relationship RBAC is default-deny when enabled. Independently, the control plane derives principal/tenant/scopes from its credential map and tenant-filters approvals and flow-status access. Static bearer tenancy is not a substitute for SSO. |
| **Secrets** | `EXAMLOPS_VAULT_ADDR` **or** `EXAMLOPS_SECRETS_KEYS`/`_ACTIVE_KEY` | Vault/OpenBao KV → else envelope-encrypted keyring with online KEK rotation (`exa secrets rewrap`). `DASHBOARD_SECRET_KEY` is the legacy decrypt-only fallback. |
| **Config location** | `EXAMLOPS_CONFIG` (file) / `EXAMLOPS_CONFIG_DIR` (dir) | Point CLI + services at a shared, mounted config dir (`config.toml`, `finops.yaml`, `policy.yaml`). |
| **Host scoping** | `CONTROL_PLANE_ALLOWED_HOSTS` | Lock the control plane's Host-header allow-list (default `*` = dev). |
| **HPC scheduler** | `EXAMLOPS_HPC_SCHEDULER=mock\|slurm\|flux` + `clusters.yaml` | Neutral scheduler abstraction; registry + approval gate via `exa hpc`. |
| **Observability** | `OTEL_SDK_DISABLED=false` + OTLP endpoint | Traces to Tempo; Prometheus scrapes the `/metrics` endpoints. |

---

## Path C — CI auto-deploy to a node (how LXP is deployed)

The active production deploy today. On merge to `main`, GitLab CI (`.gitlab-ci.yml`) runs
`sanity → test → deploy:lxp → smoke:lxp`:
1. `deploy:lxp` SSHes to `lxp-cpu01` (keys from CI vars), records the previous SHA, `git pull`s
   `$EXAMLOPS_DEPLOY_PATH`, refreshes the host `exa` install, and runs
   `docker compose -f docker-compose.yml -f docker-compose.lxp.yml build && up -d`.
2. `smoke:lxp` probes the services and **auto-rolls back to the previous SHA** on failure.

Manual fast-path (when CI is slow/queued) — pull + rebuild the changed service on the node:
```bash
ssh -o RemoteCommand=none -o ClearAllForwardings=yes lxp \
  "cd $EXAMLOPS_DEPLOY_PATH && git pull --ff-only && make dashboard-up"   # or stack-up / control-plane-up
```
> The `lxp` alias forces a port-forward RemoteCommand; run remote commands with
> `-o RemoteCommand=none -o ClearAllForwardings=yes` (or use the `lxp-cpu01` alias).

**Recorded deployment — Platform Ops (2026-07-30):** the platform-management façade + workbench +
dashboard console + `exa modelzoo adopt` were deployed to `lxp-cpu01` via this path (GitLab `4eb4068`),
the dashboard was rebuilt, and the live API was verified end-to-end (authenticated
`/api/v1/platform-ops/overview` returns the real cost card + change feed). See ADR 0098 and
`docs/guides/platform-ops-workbench.md`.

---

## What a *true* one-command enterprise cluster install still needs

The gap list below does not block Path A; these items stand between the current
partial Helm chart and a turnkey, HA, multi-tenant cluster install:

1. **Versioned artifacts, not build-from-HEAD** — *done; first published as v0.54.0 (ADR 0129).*
   `.github/workflows/release.yml` turns a tag into the `examlops` wheel (PyPI, Trusted Publishing),
   seven signed and scanned GHCR images, the Helm chart as a signed OCI artifact, and a GitHub
   Release with SBOMs and checksums ([Releases](release-process.md)). Outstanding: PyPI, which
   needs the owner-side Trusted Publisher before `pip install examlops` works.
2. **Complete the Helm chart.** *Done: a strict `values.schema.json`, opt-in per-tier default-deny
   `NetworkPolicy`, and a control-plane `ServiceMonitor` (see the chart README).* Still to add: the
   missing tiers (MLflow, Prefect, Ray Serve, MinIO, JupyterHub), a `PrometheusRule`, and either bundle the stateful
   services as subcharts (Postgres/Redis operators) or ship an **umbrella chart** so the data
   layer isn't fully bring-your-own. Bump `appVersion` to match code.
3. **Finish multi-replica control-plane coordination.** The platform, dashboard, and control-plane
   state can use Postgres, and durable command claims plus Prefect idempotency keys protect dispatch.
   Rate limits, retrain locks, and singleton poller ownership already use the selected DB/Redis
   coordinator, and the in-service relay can publish the shared outbox through Redis Streams.
   Prefect circuit-breaker state and runtime ModelZoo configuration are still process-local;
   requests without a client idempotency key intentionally create new commands. Add shared/runtime
   configuration semantics and pass concurrent-replica and failover tests before enabling the HPA.
   See `docs/guides/postgres-backend.md`.
4. **Identity on by default.** Wire the OIDC dependency across control-plane/dashboard/agent routes and
   ship multi-tenancy as the enterprise default, replacing the two-shared-passwords model.
5. **A cluster bootstrapper.** A Terraform module / operator (or the umbrella chart above) that stands
   up the managed data services + secret store, so "install on a brand-new cluster" is one documented
   command — not manual Postgres/MinIO/Redis provisioning.
6. **Secure-by-default.** Remove the `minioadmin` defaults, plaintext HTTP, and wildcard
   CORS/allowed-hosts from the compose path so a copy-paste install isn't insecure.

These map to the public architecture records: Phase 1 HA, Phase 2 identity and
tenancy, followed by automated cluster bootstrap and operational hardening.

---

## Quick decision guide

- **New laptop / single VM / demo / air-gapped box** → **Path A** (`make bootstrap`). Ready today.
- **Existing K8s cluster, you already run managed Postgres/MinIO/Redis** → **Path B** (Helm), and plan
  for gaps #3–#4 (state re-platform + identity) before trusting it for multi-tenant HA.
- **Brand-new cluster from scratch, turnkey** → not one command yet; close gaps #1, #2, #5 first (or run
  Path A on a single large node as an interim production footprint).
