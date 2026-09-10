# ExaMLOps Helm chart — enterprise reference topology

> Enterprise-readiness Phase 1, item 1.1. Docker Compose stays **dev-only**; this chart is the
> target production shape behind an ingress, with state in HA Postgres and distributed MinIO. The
> control plane defaults to one replica while its Prefect circuit breaker/runtime config remain
> process-local and multi-replica failover is unverified. It renders and validates today (`helm lint`, `helm template`,
> `kubectl apply --dry-run`); wiring it to a live cluster + the managed data services is the
> operator step.

## What it deploys

| Tier | Kind | Scaling | HA |
|---|---|---|---|
| control-plane | Deployment + Service | 1 replica (safe default) | readiness/liveness split, topology spread |
| dashboard | Deployment + Service | `dashboard.replicaCount` (2) | PDB minAvailable 1 |
| agent | Deployment + Service | 2 replicas by default; optional CPU HPA | PDB minAvailable 1, topology spread |
| ingress | Ingress (TLS) | — | terminates TLS; dashboard owns `/api`, explicit machine paths reach the control plane |

Every pod is **non-root, read-only-rootfs, all caps dropped, no privilege escalation**
(`podSecurityContext`/`containerSecurityContext`), spread across nodes (`topologySpreadConstraints`),
and carries **no `container_name` pins or hostPath** — pods are freely schedulable and replaceable,
which is exactly what the audit flagged the Compose topology could not do.

The agent HPA is intentionally off by default, so `agent.replicaCount: 2` remains authoritative.
Enable it only after sizing the agent and its external model backend; CPU utilization reflects the
agent pod, not provider-side capacity:

```yaml
agent:
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 6
    targetCPUUtilizationPercentage: 70
  pdb:
    enabled: true
    minAvailable: 1
```

Conversation checkpoints use shared Postgres. Long-term semantic memory is still SQLite-only, so
the chart sets `AGENT_MEMORY_ENABLED=false`; otherwise each replica would expose a different,
ephemeral memory store. Keep it disabled until a shared long-term store is available.

## State lives outside the chart (by design)

Persistent state is provided by managed services referenced via `values.yaml`:

- **Postgres (HA):** deploy [CloudNativePG](https://cloudnative-pg.io) — a `Cluster` with 3 instances
  and synchronous replicas gives automatic failover. Put the platform connection string in the
  Secret as `EXAMLOPS_POSTGRES_DSN`; the app runs on the `EXAMLOPS_DB_BACKEND=postgres`
  StorageBackend (item 0.1). `DATABASE_URL` is a separate key used by services such as MLflow.
  ```yaml
  apiVersion: postgresql.cnpg.io/v1
  kind: Cluster
  metadata: {name: examlops-pg}
  spec: {instances: 3, storage: {size: 50Gi}}
  ```
- **MinIO (distributed):** run MinIO in distributed mode (4+ nodes, erasure coding) or point at any
  S3-compatible service; set `minio.endpoint`.
- **Redis (coordination/events):** the control plane uses the selected coordinator for rate limits,
  retrain locks, and singleton poller ownership, and its relay publishes the transactional outbox
  through the selected publisher. For cross-host operation select Redis + Redis Streams instead of
  the chart's dependency-free `db`/`log` defaults. Keep the HPA disabled until the remaining
  process-local controls and failover behavior are resolved and tested. NATS/Kafka are placeholders.

## Site modules and data upgrades (ADR 0128)

- **`site.features`** — the centre's [site feature profile](../../../../docs/guides/site-feature-profiles.md),
  injected into every pod as `EXAMLOPS_FEATURES` (`""` = every module). Generate it, together with
  **`agent.enabled`** (set `false` to not deploy the agent tier), from the site profile:
  `exa modules render --target helm --out site-values.yaml`, then `-f site-values.yaml`.
- **`upgrade.hook.enabled`** — a `pre-install,pre-upgrade` Job running `exa upgrade apply` with the
  control-plane image, so the datastore is at the new release's data format before any new pod
  starts (a failed migration fails the `helm upgrade`). Online migrations also apply on first open;
  the hook matters for releases with offline ones. It does not back up Postgres — take a
  CloudNativePG backup first. See [Upgrades & compatibility](../../../../docs/guides/upgrade-and-compatibility.md).
- Every tier gets `EXAMLOPS_DEPLOYMENT=kubernetes`, which `exa instance info` reports.

## Secrets — never templated

The chart references existing Kubernetes Secrets and never embeds their values. Existing releases
remain compatible: when the per-tier settings below are empty, every Deployment falls back to the
global `existingSecret` (default `examlops-secrets`). For new deployments, use separate Secrets so a
compromised pod cannot read credentials belonging only to another tier:

```yaml
existingSecret: examlops-secrets # upgrade fallback; can remain during migration
controlPlane:
  existingSecret: examlops-control-plane-secrets
dashboard:
  existingSecret: examlops-dashboard-secrets
agent:
  existingSecret: examlops-agent-secrets
```

Create these objects out-of-band with External Secrets, Sealed Secrets, or your cluster's secret
manager. Never commit Secret data or place credentials in a values file. Start with only the keys
needed by each enabled capability:

| Secret | Baseline keys | Add only when enabled |
|---|---|---|
| Control plane | `CONTROL_PLANE_TOKEN` or `CONTROL_PLANE_CREDENTIALS_JSON`; `EXAMLOPS_POSTGRES_DSN` | Redis/event credentials, ModelZoo webhook secret, GitLab trigger credentials |
| Dashboard | `DATABASE_URL`, `EXAMLOPS_POSTGRES_DSN`, `DASHBOARD_JWT_SECRET`, `DASHBOARD_SECRET_KEY`, `DASHBOARD_ADMIN_PASSWORD`, `DASHBOARD_VIEWER_PASSWORD`, `DASHBOARD_AGENT_API_KEY` | `CONTROL_PLANE_TOKEN`, `EXAMLOPS_SECRETS_KEYS`, MinIO/GitLab/JupyterHub credentials |
| Agent | `AGENT_API_KEYS_JSON`, `AGENT_POSTGRES_DSN` | One cloud LLM provider credential set (none for Ollama), `CONTROL_PLANE_TOKEN` for write tools, `DASHBOARD_ADMIN_PASSWORD` for dashboard tools, `AGENT_ACTION_SIGNING_KEY` |

`CONTROL_PLANE_CREDENTIALS_JSON` is keyed by bearer secret; each value declares a trusted
`principal`, `tenant`, and `scopes` list containing `read` and/or `write`. Give the dashboard and
each automation caller a distinct entry and place only that caller's bearer value in its own Secret.
The legacy `CONTROL_PLANE_TOKEN` maps to `legacy/default` with both scopes. Malformed structured
configuration fails closed, including for the legacy credential.

For Azure, the provider set is `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, and
`AZURE_OPENAI_DEPLOYMENT`; Anthropic needs `ANTHROPIC_API_KEY`. Give the dashboard and each CLI
operator different agent credentials. The keys in `AGENT_API_KEYS_JSON` are authenticated principal
names used to own agent sessions and memory. Configure an operator's local CLI with the hidden
`exa config set agent_token` prompt. Existing installations may keep `AGENT_API_KEY`; the dashboard
uses it only when `DASHBOARD_AGENT_API_KEY` is absent.

Use either the shown Azure credentials, `ANTHROPIC_API_KEY`, or override
`agent.extraEnv` with a reachable Ollama URL. The agent pod's own `localhost` is not the host.

## Install / validate

`global.imageRegistry` is **required** and every command below needs it — the chart refuses to
render without one, because an unqualified image name resolves to `docker.io/library/`, which only
Docker can publish to. Note the trailing slash. `helm lint` is the exception that misleads: it
prints the failure and still reports `0 chart(s) failed`, so never read a bare lint as a pass.

```bash
REG=ghcr.io/<owner>/          # the registry holding the ExaMLOps images — trailing slash

helm lint platform/infra/helm/examlops --set global.imageRegistry=$REG
helm template rel platform/infra/helm/examlops --set global.imageRegistry=$REG \
  | kubectl apply --dry-run=client -f -                                  # schema check
helm upgrade --install examlops platform/infra/helm/examlops \
  -n examlops --create-namespace \
  --set global.imageRegistry=$REG \
  --set ingress.host=examlops.example.org --set global.cluster=prod
```

Create every selected Secret **before** installing: each Deployment references its resolved Secret
with a non-optional `envFrom.secretRef`, so a missing per-tier override or fallback prevents that pod
from starting.

`make helm-validate` runs the lint + render + dry-run gate, and it passes the registry — which is
why it stayed green while the commands in this section did not work.
