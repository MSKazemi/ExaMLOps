# Enterprise Installation & Configuration

How to install and configure ExaMLOps on a new machine, cluster, or environment — and an honest map
of what is production-ready today versus what a fully enterprise, one-command cluster install still
needs. Every path and knob below is grounded in the actual repo (Makefile targets, compose files, the
Helm chart, `.env.example`, and the `examlops.*` config seams).

## Maturity at a glance

| Install path | What it is | Status |
|---|---|---|
| **A — Single node (Docker Compose)** | `make bootstrap` → uv venv + `docker compose up` | **Production-in-use** (this is what runs on `lxp-cpu01`). Best path today for a new machine or single VM. |
| **B — Kubernetes (Helm)** | `helm install` control-plane + dashboard + agent | **Partial / reference.** Renders, lints, dry-runs, enterprise pod-security — but covers only 3 tiers and assumes you bring your own Postgres/MinIO/Redis/NATS. |
| **C — CI auto-deploy (GitLab → node)** | `deploy:lxp` SSHes to the node, `git pull`, rebuilds, smoke-gated auto-rollback | **Production-in-use, single-node.** Deploy-from-HEAD to one NFS host; no image registry, no canary. |

**Bottom line:** for a *new computer or single server* you can be fully running in ~15 minutes via
Path A. For a *brand-new multi-node enterprise cluster* the pieces exist as seams and a partial Helm
chart, but there is real gap-closing work (below) before it is a turnkey, HA, multi-tenant install.

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
#   = touch-env-dashboard + stack-up (docker compose up -d --build) + install-dev (uv pip install -e ".[dev]")

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

The chart at `platform/infra/helm/examlops/` (Chart 0.1.0). It ships a genuinely enterprise **pod
posture** — non-root (uid 10001), read-only rootfs, drop-ALL caps, seccomp RuntimeDefault, HPA + PDB +
topology-spread — and an nginx Ingress with TLS. **It deploys only control-plane + dashboard + agent**
and expects the stateful services to be provided externally.

### Prerequisites (bring-your-own managed services)
- A Kubernetes cluster (1.27+), an ingress controller, and cert-manager (or pre-provisioned TLS).
- **Postgres** (the chart references CloudNativePG — `postgres.host/readHost/database`).
- **Object storage** (MinIO or S3 — `minio.endpoint/bucket`).
- **Redis** (for cross-host coordination) and **NATS** (event backbone) — the chart injects
  `EXAMLOPS_COORDINATOR=redis` and `EXAMLOPS_EVENT_PUBLISHER=nats`.
- An external **Secret** holding the app secrets (never templated into the chart).

### Install
```bash
# 1. Create the app secret out-of-band (see the chart README for the full key list)
kubectl create secret generic examlops-secrets \
  --from-literal=DASHBOARD_JWT_SECRET=... \
  --from-literal=DASHBOARD_SECRET_KEY=... \
  --from-literal=DASHBOARD_ADMIN_PASSWORD=... \
  --from-literal=DASHBOARD_VIEWER_PASSWORD=... \
  --from-literal=CONTROL_PLANE_TOKEN=...

# 2. Validate then install
make helm-validate                       # lint + template + kubectl --dry-run
helm install examlops platform/infra/helm/examlops -f my-values.yaml
```

### Day-0 enterprise configuration (the env seams to turn on)
These are the flags that flip ExaMLOps from single-tenant dev to multi-tenant HA. They exist as
`examlops.*` seams with a working default and a loud-failing enterprise backend:

| Concern | Env / setting | Notes |
|---|---|---|
| **Data backend** | `EXAMLOPS_DB_BACKEND=postgres` + `EXAMLOPS_POSTGRES_DSN` | Working: all helpers reach it through `platform_db.get_db()`, which the backend now fronts (`examlops.storage.pg`). Verified live on Postgres 16 — schema, audit hash chain, append-only triggers. Not yet pooled, and the full unit suite has not been run on it: see [Postgres backend](postgres-backend.md). |
| **Coordination** | `EXAMLOPS_COORDINATOR=redis` + `EXAMLOPS_REDIS_URL` | Cross-host leader/lease election; `db` (default) works cross-process on one node. |
| **Event backbone** | `EXAMLOPS_EVENT_PUBLISHER=nats` (or `kafka`/`redis`) | Transactional outbox; drain with `exa events relay`. `log` is the dependency-free default. |
| **Identity / SSO** | `EXAMLOPS_OIDC_ISSUER` / `_AUDIENCE` / `_JWKS` (`examlops[oidc]`) | RS256 access-token validation. **Off by default** → the only identity is the two dashboard passwords + control-plane token. Turn this on for enterprise. |
| **Multi-tenancy** | `EXAMLOPS_MULTITENANCY=1` | Default-deny relationship RBAC (`owner⊇editor⊇viewer`) over `authz_relations`; OpenFGA is a swap-in. Off by default (single-tenant allow). |
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

The honest gap list (source: `.claude/plans/enterprise-readiness/`). None of these block Path A; they
are what stands between "partial Helm chart" and "turnkey, HA, multi-tenant cluster install":

1. **Versioned artifacts, not build-from-HEAD.** Today every path builds images on the target host
   (`compose build`) and installs the CLI editable (`pip install -e`). Enterprise needs **published,
   signed container images** (a registry + a release job) and a **published Helm chart / wheels** so a
   cluster pulls immutable, versioned artifacts. *(Add an image-build+push CI job; the packages are
   already wheel-buildable via setuptools, just not distributed.)*
2. **Complete the Helm chart.** Add the missing tiers (MLflow, Prefect, Ray Serve, MinIO, JupyterHub),
   `NetworkPolicy`, `ServiceMonitor`/`PrometheusRule`, agent HPA/PDB, and either bundle the stateful
   services as subcharts (Postgres/Redis/NATS operators) or ship an **umbrella chart** so the data
   layer isn't fully bring-your-own. Bump `appVersion` to match code.
3. **Re-platform state for real.** `EXAMLOPS_DB_BACKEND=postgres` now carries every `platform_db`
   helper and the whole unit suite passes on Postgres 16 — what is left before multi-replica HA is
   real: connection pooling, the **dashboard** (it still connects by SQLite *path*, so it would read
   empty state), and a `pg_dump` backup tier. Same for the Redis coordinator and NATS/Kafka event
   backbone (currently loud-failing skeletons). See `docs/guides/postgres-backend.md`.
4. **Identity on by default.** Wire the OIDC dependency across control-plane/dashboard/agent routes and
   ship multi-tenancy as the enterprise default, replacing the two-shared-passwords model.
5. **A cluster bootstrapper.** A Terraform module / operator (or the umbrella chart above) that stands
   up the managed data services + secret store, so "install on a brand-new cluster" is one documented
   command — not manual Postgres/MinIO/Redis/NATS provisioning.
6. **Secure-by-default.** Remove the `minioadmin` defaults, plaintext HTTP, and wildcard
   CORS/allowed-hosts from the compose path so a copy-paste install isn't insecure.

These map directly onto the enterprise-readiness roadmap (Phase 1 HA → Phase 2 identity/tenancy → …) in
`.claude/plans/enterprise-readiness/02-ROADMAP.md`.

---

## Quick decision guide

- **New laptop / single VM / demo / air-gapped box** → **Path A** (`make bootstrap`). Ready today.
- **Existing K8s cluster, you already run managed Postgres/MinIO/Redis** → **Path B** (Helm), and plan
  for gaps #3–#4 (state re-platform + identity) before trusting it for multi-tenant HA.
- **Brand-new cluster from scratch, turnkey** → not one command yet; close gaps #1, #2, #5 first (or run
  Path A on a single large node as an interim production footprint).
