# Control Plane

The ExaMLOps control plane (`platform/services/control_plane/app.py`) is the command and approval
gateway in front of Prefect. It validates retrain requests, persists approval and ModelZoo freshness
state, and dispatches accepted work. Training, inference, model promotion, and most platform domain
state remain outside this service.

## Responsibilities

- Validate model and dataset names against the active use-case registry before calling Prefect.
- Store and resolve pending model-change approvals.
- Receive authenticated GitLab/GitHub ModelZoo webhooks and track model freshness.
- Poll ModelZoo when enabled, expose service diagnostics, and publish approval/retrain metrics.
- Serve read-only model metadata and README/image assets without importing training code.

State is selected with `EXAMLOPS_DB_BACKEND`. SQLite at `CONTROL_PLANE_DB` is the local default;
Postgres uses `EXAMLOPS_POSTGRES_DSN` and shares approvals, events, and freshness across processes.
Postgres removes the private SQLite persistence island, but it does **not** by itself permit safe
multi-replica control-plane operation.

## Runtime topology and maturity

```text
CLI / dashboard / CI webhooks
             │
             ▼
     Control Plane :18002
       ├─ SQLite or Postgres state
       ├─ durable command claims, dispatch records, admission records, and event outbox
       ├─ configured DB/Redis coordinator for rate limits and leases
       ├─ leased ModelZoo poller and in-process Prefect circuit breaker
       ├─ transactional outbox relay to the configured publisher
       └─ PrefectGateway ──► Prefect ──► training flows
```

Retrain and approval dispatches use durable command keys, expiring dispatch leases, and Prefect
idempotency keys. A completed command can replay its stored response, and a replacement process can
recover an expired dispatch without deliberately creating a second flow run. Command completion,
approval state, its admission record, and its outbox event are committed together. The outbox relay
is at least once; consumers deduplicate with the stable event ID.

Rate limiting and retrain/poller ownership use the configured shared coordinator. SQLite with the DB
coordinator is host-local; Postgres or Redis is required for cross-host coordination. The Prefect
circuit breaker and runtime ModelZoo configuration remain process-local, and retrains without a
caller idempotency key intentionally create a new command. `/health` therefore reports
`runtime.horizontal_scaling_safe: false` and names the active blockers. Keep the Helm control-plane
replica count at one until failover behavior is tested and the remaining blockers are removed.

## Health and readiness

| Endpoint | Meaning |
|---|---|
| `GET /livez` | Process liveness. Use for restart decisions. |
| `GET /readyz` | Traffic readiness. Returns 503 when startup checks, approval storage, or an enabled poller are unhealthy. |
| `GET /ready` | Compatibility alias for the original liveness endpoint. |
| `GET /health` | Diagnostic JSON. Always inspect its `status`, `startup_checks`, `poller`, and `runtime` fields. |
| `GET /status` | Concurrent peer-service probes plus the pending-approval count. |

`/health` returns HTTP 200 even when its JSON verdict is `starting` or `degraded`; it is a diagnostic
surface. Orchestrators must use `/readyz`, which encodes the verdict in the HTTP status. The Compose
healthcheck currently probes `/health`, while the Helm chart correctly separates `/livez` and
`/readyz`; use the latter pair for production scheduling.

## API and authentication

`CONTROL_PLANE_CREDENTIALS_JSON` maps bearer secrets to a server-verified `principal`, `tenant`, and
`scopes` list. `read` authorizes sensitive status, approval, and ModelZoo reads; `write` authorizes
mutations. Approval queries and mutations are tenant-filtered, and structured credentials may read
only flow runs dispatched for their tenant. The legacy `CONTROL_PLANE_TOKEN` remains compatible as
principal `legacy` in tenant `default` with both scopes. Malformed structured configuration fails
closed, including for the legacy credential. Public operational and registry surfaces are limited
to health/readiness, metrics, `/models`, and model metadata/assets.

ModelZoo webhooks use their separate `MODELZOO_WEBHOOK_SECRET`: GitLab sends `X-Gitlab-Token`, while
GitHub sends an `X-Hub-Signature-256` HMAC.

See the [Control Plane guide](../guides/control-plane.md) for the complete endpoint table, examples,
configuration, and Prometheus metrics.

## Local development

```bash
make control-plane-up
curl http://localhost:18002/livez
curl --fail http://localhost:18002/readyz
make control-plane-logs
```
