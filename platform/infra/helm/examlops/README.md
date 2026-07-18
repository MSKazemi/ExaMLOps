# ExaMLOps Helm chart — enterprise reference topology

> Enterprise-readiness Phase 1, item 1.1. Docker Compose stays **dev-only**; this chart is the
> production shape: stateless, horizontally-scalable tiers behind an ingress, with state in HA
> Postgres and distributed MinIO. It renders + validates today (`helm lint`, `helm template`,
> `kubectl apply --dry-run`); wiring it to a live cluster + the managed data services is the
> operator step.

## What it deploys

| Tier | Kind | Scaling | HA |
|---|---|---|---|
| control-plane | Deployment + Service | HPA 3→10 (CPU 70%) | PDB minAvailable 2, topology spread |
| dashboard | Deployment + Service | `dashboard.replicaCount` (2) | PDB minAvailable 1 |
| agent | Deployment + Service | `agent.replicaCount` (2) | topology spread |
| ingress | Ingress (TLS) | — | terminates TLS, routes `/api`→control-plane, `/`→dashboard |

Every pod is **non-root, read-only-rootfs, all caps dropped, no privilege escalation**
(`podSecurityContext`/`containerSecurityContext`), spread across nodes (`topologySpreadConstraints`),
and carries **no `container_name` pins or hostPath** — pods are freely schedulable and replaceable,
which is exactly what the audit flagged the Compose topology could not do.

## State lives outside the chart (by design)

The tiers are stateless; all state is in managed services referenced via `values.yaml`:

- **Postgres (HA):** deploy [CloudNativePG](https://cloudnative-pg.io) — a `Cluster` with 3 instances
  and synchronous replicas gives automatic failover. Put the connection string in the Secret as
  `DATABASE_URL`; the app runs on the `EXAMLOPS_DB_BACKEND=postgres` StorageBackend (item 0.1).
  ```yaml
  apiVersion: postgresql.cnpg.io/v1
  kind: Cluster
  metadata: {name: examlops-pg}
  spec: {instances: 3, storage: {size: 50Gi}}
  ```
- **MinIO (distributed):** run MinIO in distributed mode (4+ nodes, erasure coding) or point at any
  S3-compatible service; set `minio.endpoint`.
- **Redis + NATS (coordination/events):** once up, flip `controlPlane.env.EXAMLOPS_COORDINATOR=redis`
  and `EXAMLOPS_EVENT_PUBLISHER=nats` (endpoints in the Secret) so the control-plane replicas share
  locks/idempotency/rate-limit (item 1.2) and one event bus (item 1.3) — no double-firing.

## Secrets — never templated

The chart references an **existing** Secret (`existingSecret`, default `examlops-secrets`); it never
embeds secret values. Create it out-of-band (sealed-secrets / external-secrets / `kubectl`):

```bash
kubectl create secret generic examlops-secrets \
  --from-literal=CONTROL_PLANE_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=DASHBOARD_JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=DASHBOARD_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  --from-literal=DASHBOARD_ADMIN_PASSWORD=... --from-literal=DASHBOARD_VIEWER_PASSWORD=... \
  --from-literal=EXAMLOPS_SECRETS_KEYS="k1:$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  --from-literal=DATABASE_URL="postgresql://…"
```

## Install / validate

```bash
helm lint platform/infra/helm/examlops
helm template rel platform/infra/helm/examlops | kubectl apply --dry-run=client -f -   # schema check
helm upgrade --install examlops platform/infra/helm/examlops \
  -n examlops --create-namespace \
  --set ingress.host=examlops.example.org --set global.cluster=prod
```

`make helm-validate` runs the lint + render + dry-run gate.
