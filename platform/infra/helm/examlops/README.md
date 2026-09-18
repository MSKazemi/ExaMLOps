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
| gateway, gateway-authz (optional) | Deployment + Service each | `gateway.replicaCount` / `gateway.authz.replicaCount` (2) | PDB minAvailable 1, topology spread; see [Serving gateway](#serving-gateway-adr-0126-opt-in) |

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
  process-local controls and failover behavior are resolved and tested.
- **NATS JetStream (events):** see [Event backbone](#event-backbone-adr-0124) below.

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

**From a release** the chart and its images come from GHCR, signed — no checkout needed:

```bash
helm install examlops oci://ghcr.io/mskazemi/charts/examlops --version X.Y.Z \
  -n examlops --create-namespace --set global.imageRegistry=ghcr.io/mskazemi/
```

See [Releases](https://mskazemi.com/ExaMLOps/guides/release-process/) for verifying the chart and images with
`cosign verify` / `gh attestation verify`.

**Fixed in the working tree — the control plane no longer stays unready after a first install.**
- **Cause:** on a first install against an empty Postgres, every tier creates the platform schema
  at the same moment, and the control plane can lose that race. Its log shows `Startup check
  FAILED — coordinator: duplicate key value violates unique constraint
  "pg_type_typname_nsp_index"`.
- **What made it fatal** was not losing the race — the table exists a second later — but that the
  startup checks ran **once at boot and never again**, so one unlucky moment pinned the replica
  NotReady for its whole life and `helm install --wait` timed out.
- **The fix:** a failing check is re-evaluated by the probes themselves, rate-limited to one
  battery every `CONTROL_PLANE_STARTUP_RECHECK_SECONDS` (default 10). Checks that passed are not
  re-run, so a probe stays cheap. The pod therefore becomes `1/1` on its own, within about ten
  seconds, with no restart.
- **Losing the race is still logged**, and that is deliberate: a first install that recovers by
  itself should still leave evidence that the tiers collided.
- **Pinned by** `tests/test_startup_recheck.py` — including that **`/readyz`** itself flips 503 →
  200, which is the endpoint the chart's `readinessProbe` calls and therefore the only place the
  recovery is visible to Kubernetes.
- **Measured on v0.55.0 on kind before the fix:** three fresh installs with the images already on
  the node all hung this way, and one `kubectl rollout restart` fixed each. If you are running a
  published chart from **v0.54.0–v0.56.0**, that restart is still the workaround — the fix is in the
  working tree, not yet in a release.

## Values are validated

`values.schema.json` is enforced by helm on install, upgrade, lint and template. Every object the
chart owns rejects unknown keys, so `--set controlPlane.replicaCont=2` fails with the misspelt
key named instead of being silently ignored; ports, pull policies, replica counts and the trailing
slash on `global.imageRegistry` are checked too. Add a value → add it to the schema in the same
change, or `helm lint` fails.

## Event backbone (ADR 0124)

Every platform change is written to a transactional outbox with the change itself. With
`events.publisher: log` (the default) the control plane's relay only logs those events, and nothing
consumes them. With `nats` it publishes them to NATS JetStream, where the dashboard's live stream
and the followers below read them. NATS is external, like Postgres:

```yaml
events:
  publisher: nats
  natsUrl: nats://nats.messaging:4222   # a JetStream-enabled NATS server
  followers:
    autopilot: {enabled: true}      # `exa autopilot follow`: a model's autopilot cycle when its training run completes
    skipperWatch: {enabled: true}   # `python -m skipper.watch --daemon`: alert.retrain when a run fails
```

- The publisher and `EXAMLOPS_NATS_URL` reach every tier through the shared ConfigMap.
  `controlPlane.env` no longer sets the publisher: a container `env` entry overrides the
  ConfigMap, so it would have kept the control plane on `log` whatever `events` said.
- Each follower is a Deployment with one replica, with no overlap during a rollout, because a second
  pod would share the durable consumer's work. It has no Service and the same pod and container
  hardening as every tier, with `HOME=/tmp` on the read-only root filesystem.
- The autopilot follower sends the control plane `AUTOPILOT_CONTROL_PLANE_TOKEN` from the
  control-plane Secret when that key exists. Give it only the `retrain` scope in
  `CONTROL_PLANE_CREDENTIALS_JSON`. When the key is absent, it uses the Secret's shared token.
- The render fails for `publisher: nats` without `natsUrl`, and for a follower enabled without the
  `nats` publisher, which would deploy a pod that nothing can ever reach.

## Serving gateway (ADR 0126, opt-in)

`gateway.enabled` deploys the serving gateway, as Compose's `gateway` profile does. It has two
tiers: Envoy, the one front door to the model server, and the authorization service
(`examlops.serving_gateway`, control-plane image) that checks virtual keys and IdP tokens and sets
the verified tenant.

- **One Envoy configuration.** `files/gateway-envoy.yaml` is byte-identical to Compose's
  `gateway/envoy.yaml` (tests/unit/test_helm_gateway.py). Only the model server's addresses
  (`gateway.upstream.host`, its REST `port` and its gRPC `grpcPort`, 8081) and the authorization
  service's are substituted, and a change rolls the Envoy pods. The gateway serves the Open Inference
  Protocol over REST and gRPC on its one port.
- **Resilience and hardening.** Two replicas and a PodDisruptionBudget per tier, the chart's pod
  and container security, Envoy with `--disable-hot-restart`, and TCP probes.
- **Optional extras:**
    - its own ingress host (`gateway.ingress.host`) on the chart's Ingress and certificate;
    - NetworkPolicy tiers: clients through the ingress controller; only the gateway may call the
      authorization tier;
    - a ServiceMonitor for Envoy's statistics.
- **Tested in kind:** `tests/integration/test_helm_gateway_kind_live.py`.

## Workload identities (ADR 0125, opt-in)

Each tier can prove who it is to the control plane with a five-minute JWT-SVID from SPIRE, with its
static credential as the fallback. SPIRE is cluster infrastructure, like Postgres: install it once
with SPIRE's hardened charts, which bring the server, the node agents, the SPIFFE CSI driver and
spire-controller-manager:

```bash
helm upgrade --install -n spire-mgmt --create-namespace spire-crds spire-crds \
  --repo https://spiffe.github.io/helm-charts-hardened/
helm upgrade --install -n spire-mgmt spire spire \
  --repo https://spiffe.github.io/helm-charts-hardened/ \
  --set global.spire.namespaces.create=true --set global.spire.trustDomain=example.org
```

Then:

```yaml
workloadIdentity:
  enabled: true
  trustDomain: example.org   # the SPIRE server's
```

- **One `ClusterSPIFFEID` per tier** (control plane, dashboard, the agent and the autopilot follower
  when enabled). Each selects its own pods by `app.kubernetes.io/{name,instance,component}` in the
  release namespace, and names them
  `spiffe://<trustDomain>/ns/<namespace>/<release>-examlops/<tier>`. They are not `fallback`, so
  they win over SPIRE's default per-service-account identity. With
  `clusterSPIFFEID.create: false`, register the same IDs another way.
- **A `spiffe-helper` sidecar in each of those pods** reaches the Workload API through the CSI
  driver: no hostPath, same user, read-only root and dropped capabilities as the tier. It keeps
  the tier's token in a memory volume that the tier mounts read-only and reads through
  `CONTROL_PLANE_TOKEN_FILE`; the control plane's helper keeps the trust bundle.
- **The control plane's map of who may do what** (`CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON`) is
  rendered from `workloadIdentity.callers` for exactly the tiers this release deploys, from the
  same template that names the IDs. The defaults give each tier the principal and scopes its
  static credential has.
- The render fails when `enabled` has no `trustDomain`. CI validates the `ClusterSPIFFEID`s
  against a strict schema generated from SPIRE's CRD
  (`platform/infra/helm/schemas/spire.spiffe.io/`).
- Retire each static secret when its principal's `static` count in
  `control_plane_authentications_total` stops growing. See
  [Workload identity](../../../../docs/guides/workload-identity.md).

## Network isolation (opt-in)

`networkPolicy.enabled: true` puts every tier behind its own default-deny NetworkPolicy (ingress
and egress) and allows exactly these flows:

| To ↓ / from → | ingress controller | dashboard | agent | monitoring ns |
|---|---|---|---|---|
| **control-plane** | ✓ | ✓ | ✓ | ✓ (`/metrics`) |
| **dashboard** | ✓ | | | |
| **agent** | | ✓ | | |
| **autopilot-follower**, **skipper-watch** | | | | |

The autopilot follower also reaches the control plane, and the control plane accepts it. With
`events.publisher: nats`, the control plane and the dashboard may reach NATS on 4222. The
followers' own external ports are `networkPolicy.egressPorts.autopilotFollower` (NATS, Postgres,
MLflow, S3) and `.skipperWatch` (NATS, Postgres).

Every tier may resolve DNS, and reach the services this chart does not deploy on the ports in
`networkPolicy.egressPorts.<tier>` (Postgres 5432, Redis 6379, Prefect 4200, MLflow 5000, S3 9000,
Ray Serve 8001, Prometheus 9090, Ollama 11434, HTTPS 443 — trim to what your site runs), plus any
`networkPolicy.extraEgress` rules (e.g. a CIDR-scoped database). It is opt-in because it needs a
CNI that enforces NetworkPolicy and because `networkPolicy.ingressController.namespaceSelector` /
`monitoring.namespaceSelector` must match your cluster's namespace labels — a wrong label cuts a
working install off without an error. Policies are per tier, never release-wide, so the pre-upgrade
Job is not caught by them.

## Metrics (Prometheus Operator)

`metrics.serviceMonitor.enabled: true` renders a ServiceMonitor for the control plane (the only
application tier that serves `/metrics`) and, with the gateway, one each for Envoy's statistics
and the authorization tier. Add the label your Prometheus selects on
(`metrics.serviceMonitor.labels: {release: kube-prometheus-stack}`). Without the
`monitoring.coreos.com/v1` CRDs the render fails with that instruction instead of producing an
object the API server rejects. `tests/unit/test_helm_network_and_schema.py` renders all of the above
with the pinned helm.

**Alert rules.** `metrics.prometheusRule.enabled: true` renders the platform's alert rules as a
PrometheusRule:

- **Compose's rules, verbatim.** They come from `files/alert_rules.yml`, byte-identical to Compose's
  `alert_rules.yml`, which promtool checks and unit-tests in CI. Every rule links to its runbook.
- **The groups for what the chart deploys:** `examlops-control-plane` always, `examlops-gateway`
  with the gateway. Add others by name (`metrics.prometheusRule.extraGroups: [examlops-serving]`)
  for services your Prometheus scrapes itself under the same job names. An unknown name fails the
  render.
- **Job names match Compose's.** The rules select jobs such as `control_plane`, `gateway` and
  `gateway_authz`. With the Prometheus Operator a job is otherwise the Service's name, and those
  rules would match nothing and never fire. So each Service carries `examlops.io/job`, each
  ServiceMonitor uses it as its `jobLabel`, and `tests/unit/test_helm_prometheus_rules.py` checks
  that every job a rendered rule selects is one a rendered ServiceMonitor produces. A dashboard or
  query that used `job="<release>-examlops-control-plane"` now reads `job="control_plane"`.
- **Selector label.** Add the label your Prometheus' `ruleSelector` matches under
  `metrics.prometheusRule.labels`.
- **Strict validation.** CI validates the PrometheusRule and ServiceMonitors against the Prometheus
  Operator's CRD schemas (vendored in `platform/infra/helm/schemas/`).

**Tracing.** Every image runs under `opentelemetry-instrument`. Export is off by default
(`OTEL_SDK_DISABLED=true`, the compose default) so no pod retries a collector that does not exist;
`--set otel.endpoint=http://otel-collector.monitoring:4317` turns it on for every tier.
