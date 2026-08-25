# ExaMLOps Helm chart — enterprise reference topology

> Enterprise-readiness Phase 1, item 1.1. Docker Compose stays **dev-only**; this chart is the
> target production shape behind an ingress, with state in HA Postgres and distributed MinIO. The
> control plane defaults to one replica until distributed coordination is integrated. It renders
> and validates today (`helm lint`, `helm template`,
> `kubectl apply --dry-run`); wiring it to a live cluster + the managed data services is the
> operator step.

## What it deploys

| Tier | Kind | Scaling | HA |
|---|---|---|---|
| control-plane | Deployment + Service | 1 replica (safe default) | readiness/liveness split, topology spread |
| dashboard | Deployment + Service | `dashboard.replicaCount` (2) | PDB minAvailable 1 |
| agent | Deployment + Service | `agent.replicaCount` (2) | topology spread |
| ingress | Ingress (TLS) | — | terminates TLS; dashboard owns `/api`, explicit machine paths reach the control plane |

Every pod is **non-root, read-only-rootfs, all caps dropped, no privilege escalation**
(`podSecurityContext`/`containerSecurityContext`), spread across nodes (`topologySpreadConstraints`),
and carries **no `container_name` pins or hostPath** — pods are freely schedulable and replaceable,
which is exactly what the audit flagged the Compose topology could not do.

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
- **Redis + NATS (coordination/events):** these remain multi-replica prerequisites. Do not enable the
  control-plane HPA until the coordinator, poller leadership, and event consumers are integrated and
  concurrency-tested. The chart therefore keeps the implemented `db` coordinator and `log` publisher
  defaults today.

## Secrets — never templated

The chart references an **existing** Secret (`existingSecret`, default `examlops-secrets`); it never
embeds secret values. Create it out-of-band (sealed-secrets / external-secrets / `kubectl`):

```bash
kubectl create secret generic examlops-secrets \
  --from-literal=CONTROL_PLANE_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=AGENT_API_KEY="$(openssl rand -hex 32)" \
  --from-literal=AGENT_POSTGRES_DSN="postgresql://…" \
  --from-literal=DASHBOARD_JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=DASHBOARD_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  --from-literal=DASHBOARD_ADMIN_PASSWORD=... --from-literal=DASHBOARD_VIEWER_PASSWORD=... \
  --from-literal=EXAMLOPS_SECRETS_KEYS="k1:$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  --from-literal=EXAMLOPS_POSTGRES_DSN="postgresql://…" \
  --from-literal=DATABASE_URL="postgresql://…" \
  --from-literal=AZURE_OPENAI_ENDPOINT="https://<resource>.services.ai.azure.com/openai/v1/" \
  --from-literal=AZURE_OPENAI_API_KEY=... \
  --from-literal=AZURE_OPENAI_DEPLOYMENT=...
```

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

Create the `examlops-secrets` Secret **before** installing (above): the Deployments reference it
with a non-optional `envFrom.secretRef`, so without it the pods never start.

`make helm-validate` runs the lint + render + dry-run gate, and it passes the registry — which is
why it stayed green while the commands in this section did not work.
