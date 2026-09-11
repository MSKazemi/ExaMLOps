# Control Plane

`platform/services/control_plane/app.py` is the FastAPI command and approval gateway in front of
Prefect. It lets clients request retraining without holding Prefect credentials themselves. Compose
uses SQLite for local development; production can select the shared Postgres storage adapter.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/livez` | none | Process liveness for restart decisions; always 200 while the server can respond |
| GET | `/readyz` | none | Traffic readiness; returns 503 when startup checks or the approval store are unhealthy |
| GET | `/ready` | none | Compatibility alias for the original liveness endpoint |
| GET | `/health` | none | Diagnostic verdict, dependencies, pending approvals, and runtime scaling capabilities |
| GET | `/status` | **read** | Concurrent peer pings + pending approval count. Returns **exactly** `services` (`control_plane`/`mlflow`/`prefect`/`ray_serve`/`dashboard`, each `{ok, url}`) and `pending_approvals`. It carries **no** model list — `exa status` reads production models from the MLflow registry instead |
| GET | `/models` | none | List `model_name → datasets` known to the auto-discovery registry |
| POST | `/retrain` | **write** | Validate + schedule a Prefect flow run |
| GET | `/retrain/{flow_run_id}` | **read** | Poll Prefect for a run owned by the credential's tenant; the legacy credential retains operator-wide lookup |
| POST | `/api/changes` | **write** | CI webhook — record changed model IDs as tenant-scoped pending approvals (no training yet) |
| GET | `/approvals` | **read** | List only the credential tenant's approvals; filter by `?status=pending\|approved\|rejected` |
| POST | `/approve/{model_id}` | **write** | Approve a pending change in the credential tenant — fires Prefect training immediately |
| POST | `/reject/{model_id}` | **write** | Reject a pending change in the credential tenant with optional `{"reason": "..."}` body |
| POST | `/webhooks/modelzoo/gitlab` | token header | GitLab push webhook — mark models stale, optionally auto-retrain |
| POST | `/webhooks/modelzoo/github` | HMAC header | GitHub push webhook — same semantics as GitLab |
| GET | `/modelzoo/status` | **read** | Per-model freshness: `current` / `stale` / `unknown` |
| GET | `/modelzoo/events` | **read** | Recent push event history (`?limit=N`) |
| POST | `/modelzoo/sync` | **write** | Manually trigger one GitLab poll cycle |
| GET | `/modelzoo/config` | **read** | Show runtime ModelZoo config |
| PUT | `/modelzoo/config` | **write** | Update runtime config (takes effect immediately) |
| GET | `/metrics` | none | Prometheus text-format metrics for the approval gate (Phase 13) |

## POST /retrain

!!! warning "Known issue: dispatch has no matching deployment"
    `exa pipeline deploy` registers `examlops_scheduled_training/examlops-nightly`, a wrapper that
    retrains every model and accepts only `is_dummy`. It does not create
    `examlops_scheduled_training/nightly`, and no deployment it creates accepts the model and
    dataset a retrain passes. Until a per-model dispatch deployment ships, `POST /retrain` — and
    every path that calls it — fails at dispatch with Prefect's 404. The response below shows
    the shape once a matching deployment exists.

```bash
curl -X POST http://localhost:18002/retrain \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "JPCP",
    "dataset_name": "PM100Dataset",
    "is_dummy": true,
    "backend_name": "minio",
    "parameters": {"reason": "manual smoke"}
  }'
```

Response:
```json
{
  "flow_run_id": "abc123…",
  "deployment": "examlops_scheduled_training/nightly",
  "status_url": "/retrain/abc123…",
  "parameters": {
    "model_name": "JPCP",
    "dataset_cls_name": "PM100Dataset",
    "is_dummy": true,
    "backend_name": "minio",
    "reason": "manual smoke"
  }
}
```

Validation steps before the call to Prefect:

1. `model_name` must be found in the YAML registry (see [Model Registry](#model-registry) below).
2. `dataset_name` must be in that model's declared datasets.
3. The bearer credential must resolve to a configured principal with `write` scope.

Failure modes:

| HTTP | When |
|---|---|
| 400 | Unknown `model_name` or unsupported `dataset_name` |
| 401 | Missing `Authorization` header, or a token from a data-center IdP that failed verification (`WWW-Authenticate: Bearer error="invalid_token"`) |
| 403 | Wrong bearer token; a verified IdP user whose groups map to no role; or the center's PDP denied the call |
| 503 | No usable credentials are configured, or `CONTROL_PLANE_CREDENTIALS_JSON` is malformed |
| 502 | Prefect API unreachable / 5xx |

### Durable dispatch and event delivery

Each retrain or approval dispatch is claimed in `control_plane_commands` before the Prefect call.
The control plane sends the same stable idempotency key to Prefect, stores the successful response,
and can replay it for a repeated caller key. An expired dispatch lease can be recovered after a
process failure. Completion also updates its admission record and enqueues a domain event in the
same database transaction.

The built-in relay publishes the outbox through the configured publisher; operators can also run
`exa events relay` manually. `log` is the local default, while Redis Streams is the implemented
shared broker. Delivery is **at least once**, with a stable event ID for consumer deduplication.
NATS and Kafka selectors remain fail-loud placeholders.

## Approval Gate (Phase 11)

When a developer pushes to the modelzoo repo on GitHub, CI runs `ci/notify_model_changes.py`, which diffs the commits, extracts changed `model_id` values, and POSTs to `POST /api/changes`. The control plane stores these as `pending` rows in its configured state backend — **no training runs yet**.

A sysadmin then approves or rejects each change:

```bash
# List pending approvals
exa approvals list
curl http://localhost:18002/approvals?status=pending \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN"

# Approve — fires Prefect training immediately
exa approvals approve JPCP
curl -X POST http://localhost:18002/approve/JPCP \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN"

# Reject (no training)
exa approvals reject JPCP --reason "needs data review"
curl -X POST http://localhost:18002/reject/JPCP \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "needs data review"}'
```

### Approval data

Each approval record carries:

| Field | Description |
|---|---|
| `model_id` | Model identifier (e.g., `JPCP`) |
| `status` | `pending` / `approved` / `rejected` |
| `commit_sha` | Triggering commit hash |
| `commit_msg` | Commit message from CI |
| `changed_files` | JSON list of changed file paths |
| `prefect_run_id` | Prefect flow run ID (set on approval) |
| `reject_reason` | Rejection reason (set on rejection) |
| `tenant` | Verified credential tenant that owns the approval |
| `requested_by` | Verified principal that requested the approval |
| `resolved_by` | Verified principal that approved or rejected it |
| `requested_at` | ISO 8601 timestamp of the CI push |
| `resolved_at` | ISO 8601 timestamp of approve/reject |

The dashboard Approvals page (admin-only) shows a badge with the pending count and lets sysadmins approve or reject with one click.

### CI integration

The GitHub Actions `examlops` job calls `ci/notify_model_changes.py` on every push to `main`. The script:
1. Diffs `before..after` commits.
2. Finds changed files under `modelzoo/seanergys_modelzoo/models/tasks/`, `pipelines/model_configs/`, and `pipelines/models/`.
3. Extracts `model_id` values via regex.
4. POSTs to `POST /api/changes` with bearer auth.
5. **Fails silently** on connection error — never blocks CI.

Required GitHub secrets: `CONTROL_PLANE_URL` and `CONTROL_PLANE_TOKEN`.

## Auth model

`CONTROL_PLANE_CREDENTIALS_JSON` is a JSON object keyed by bearer secret. Each value defines a
server-trusted principal, tenant, and non-empty list containing `read`, `write`, or both:

```json
{
  "change-me-operator-token": {
    "principal": "release-operator",
    "tenant": "team-a",
    "scopes": ["read", "write"]
  },
  "change-me-auditor-token": {
    "principal": "auditor",
    "tenant": "team-a",
    "scopes": ["read"]
  }
}
```

The literal example secrets above are rejected as placeholders; generate distinct random values.
Identity and tenant are never accepted from request bodies or caller-chosen headers. Approval rows,
durable commands, idempotency keys, and emitted events use the verified context. Cross-tenant
approval access returns no matching record, and structured credentials cannot inspect another
tenant's flow run.

`CONTROL_PLANE_TOKEN` remains a migration-compatible operator credential. It maps to principal
`legacy`, tenant `default`, with both scopes and keeps operator-wide flow-status lookup. If the
structured JSON is malformed or reuses the legacy secret, authentication fails closed for every
credential. `/health`, readiness/liveness, metrics, `/models`, and model metadata/assets remain
public; `/status`, approval, flow-status, and ModelZoo operational reads require `read`.

### Federated users (ADR 0120)

With a trust file (`EXAMLOPS_IAM_CONFIG`), the control plane also accepts **access tokens from a
trusted data-center IdP** alongside the static credentials above, which are checked first: a
bearer that matches no static secret and is a JWT (or an opaque token named with the
`X-ExaMLOps-IdP: <provider>` header) is verified against that center's keys and audience. The
principal is `<provider>:<sub>`, the tenant is the one the trust file binds to that issuer, and the
role the center's groups map to decides the scopes: `viewer` → `read`, `operator`/`admin` →
`read` + `write`. When the center's entry sets `authorization.mode: both`, every scoped route also
asks the center's AuthZEN/OPA PDP about `api.read`/`api.write` on the route path, and a PDP outage
denies. Federation alone is a complete configuration (no `CONTROL_PLANE_TOKEN` needed); an invalid
trust file refuses every federated token and shows as `identity_federation: fail: …` in `/health`
startup checks. Users get a token with `exa auth login`; see
[Identity federation](identity-federation.md).

## Wiring with the dataplane simulator

The dataplane simulator (`platform/clients/dataplane_sim.py`) gains its own `POST /trigger-retrain` that proxies to the control plane:

```bash
curl -X POST http://localhost:8010/trigger-retrain \
  -H "Content-Type: application/json" \
  -d '{"model_name":"JPCP","dataset_name":"PM100Dataset","reason":"drift>50%"}'
```

That endpoint is what the Phase 4 client-sim drift tracker calls when its rolling per-model error rate exceeds `CLIENT_SIM_DRIFT_THRESHOLD`. The dataplane forwards with the configured `CONTROL_PLANE_TOKEN`.

## Make targets

```bash
make control-plane-up                         # start the service on :18002
make control-plane-down                       # stop it
make control-plane-logs                       # tail logs
exa retrain JPCP --dataset PM100Dataset --dummy
                                              # one-shot POST /retrain via curl
```

## Model Registry

`GET /models` and `POST /retrain` validation both rely on the active use-case pack. The control plane
resolves its model YAML directory from `EXAMLOPS_USECASE_DIR`/`usecases/seanergy/pack.toml`; it does
**not** import model training classes.

This approach (`_load_registry()` in `app.py`) avoids importing model training code. The parsed
registry has a 60-second in-process TTL; `POST /admin/reload` invalidates it immediately.

`model_meta.py` follows the same pattern: it scans YAML files for metadata (task type, schema, promotion rules, serving aliases) and locates the model's source directory by text-scanning the modelzoo for `class <ModelClass>` — never importing the class itself.

## ModelZoo Integration (Phase 12)

The control plane is the authoritative hub for ModelZoo repository freshness tracking. It receives push events from GitLab/GitHub webhooks and from a background poller, records them in its configured state backend, and exposes freshness state via REST endpoints. The dashboard and `exa` CLI both consume these endpoints.

### How it works

1. **Push event arrives** — via `POST /webhooks/modelzoo/gitlab` (or `/github`), or discovered by the background poller querying the GitLab API.
2. **Event recorded** — a row is inserted into `modelzoo_events` (commit SHA, branch, pushed_by, timestamp, source).
3. **All models marked stale** — every model in the auto-discovery registry gets an upserted row in `model_freshness` with `is_stale=1` and the current timestamp as `stale_since`.
4. **Optional auto-retrain** — if `_modelzoo_config["auto_retrain"]` is true, `POST /retrain` fires for each model with its first supported dataset.
5. **Freshness update** — the current automatic path clears `is_stale` after Prefect accepts the
   retrain request and stores that commit as `last_retrain_commit`. This records dispatch, not
   successful training completion. The manual approval path does not currently reconcile freshness;
   consumers must not treat `current` as proof that a training run completed successfully.

### Webhook registration

**GitLab** — Settings → Webhooks → add URL `http://<control-plane>:18002/webhooks/modelzoo/gitlab`, select *Push events*, set secret token to `MODELZOO_WEBHOOK_SECRET`.

**GitHub** — Settings → Webhooks → add URL `http://<control-plane>:18002/webhooks/modelzoo/github`, content type `application/json`, select *Push events*, set secret to `MODELZOO_WEBHOOK_SECRET`.

The dashboard Config page (ModelZoo Integration section) shows the pre-filled webhook URL derived from the live control plane address.

### Background poller

The poller runs as a daemon thread inside the control-plane process. FastAPI lifespan starts it, and
an interruptible event wait applies interval changes on the next boundary and permits clean shutdown.
Only the replica holding the coordinator lease polls. Keep one replica until failover is tested and
the remaining process-local circuit-breaker/runtime-config blockers are removed.

Disable polling with `MODELZOO_POLL_SECONDS=0` (env var) or `PUT /modelzoo/config {"poll_interval_seconds": 0}` at runtime.

### Runtime config

`PUT /modelzoo/config` writes to the `_modelzoo_config` in-memory dict (protected by `_CONFIG_LOCK`). Both the poller loop and the webhook handlers read from this dict, so changes are live immediately:

```bash
# Flip on auto-retrain without restarting the service
curl -X PUT http://localhost:18002/modelzoo/config \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"auto_retrain": true}'

# Halve the poll interval
curl -X PUT http://localhost:18002/modelzoo/config \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" \
  -d '{"poll_interval_seconds": 150}'
```

### `exa modelzoo` CLI

The `exa modelzoo` command group talks directly to these endpoints:

```bash
exa modelzoo status               # table: model, status (CURRENT/STALE), stale since, latest commit
exa --json modelzoo status        # raw JSON list
exa modelzoo events               # table: id, commit, branch, pushed_by, timestamp, source
exa modelzoo events --limit 5     # last 5 events
exa modelzoo sync                 # trigger one poll cycle, print result
exa modelzoo config               # show auto_retrain, poll_interval_seconds, watch_branch
```

### State tables

Two tables are created on first startup alongside `pending_approvals` in the configured SQLite or
Postgres state backend:

**`modelzoo_events`**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto-increment |
| `commit_sha` | TEXT | Git commit SHA |
| `branch` | TEXT | Branch name |
| `pushed_by` | TEXT | GitLab username / GitHub login / Git author |
| `timestamp` | TEXT | ISO 8601 |
| `source` | TEXT | `webhook` or `poll` |

**`model_freshness`**

| Column | Type | Notes |
|---|---|---|
| `model_id` | TEXT PK | e.g. `JPCP` |
| `latest_modelzoo_commit` | TEXT | Latest push SHA |
| `last_retrain_commit` | TEXT | SHA attached to the last accepted automatic retrain dispatch; not completion proof |
| `is_stale` | INTEGER | 1 = stale, 0 = current |
| `stale_since` | TEXT | ISO 8601 when stale flag was set |
| `retrain_triggered_at` | TEXT | ISO 8601 of last auto-retrain trigger |

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_PORT` | `8002` | HTTP port |
| `CONTROL_PLANE_TOKEN` | unset | Legacy `legacy/default` bearer credential with `read` + `write`; optional when the structured map is configured |
| `CONTROL_PLANE_CREDENTIALS_JSON` | unset | Token-keyed JSON map of `principal`, `tenant`, and `scopes`; malformed input fails all bearer authentication closed |
| `PREFECT_API_URL` | `http://localhost:14200/api` | Prefect server endpoint. `14200` is the host port the stack publishes; under compose the service sets `http://orchestrator:4200/api` itself. |
| `PREFECT_DEPLOYMENT_NAME` | `examlops_scheduled_training/nightly` | Deployment slug `POST /retrain` schedules (must be `flow_name/deployment_name`). Known issue: `exa pipeline deploy` does not create this deployment, and none it creates accepts a retrain's `model_name` / `dataset_cls_name` — see [POST /retrain](#post-retrain) |
| `CONTROL_PLANE_URL` | `http://control-plane:8002` | Set on the dataplane simulator so it can forward |
| `EXAMLOPS_DB_BACKEND` | `sqlite` | Control-plane state engine: `sqlite` for local development or `postgres` for shared production state |
| `EXAMLOPS_POSTGRES_DSN` | unset | Required when the state backend is `postgres` |
| `CONTROL_PLANE_DB` | `/data/approvals.db` | Local SQLite path; an unwritable parent is a startup/readiness failure, never a silent fallback |
| `MODELZOO_WEBHOOK_SECRET` | unset | Shared secret for webhook HMAC/token verification |
| `MODELZOO_AUTO_RETRAIN` | `false` | Trigger auto-retrain on every push event |
| `MODELZOO_POLL_SECONDS` | `300` | GitLab poller interval (seconds); `0` disables |
| `MODELZOO_WATCH_BRANCH` | `main` | Branch watched by webhooks and poller |
| `GITLAB_PROJECT_ID` | unset | GitLab project ID or namespace/path for the poller |
| `GITLAB_TOKEN` | unset | GitLab Personal/Project Access Token (`read_repository` scope) for the poller |

## Operations

* **Liveness:** `curl localhost:18002/livez` — process-only, suitable for restart decisions.
* **Readiness:** `curl --fail localhost:18002/readyz` — non-2xx when the replica must not receive traffic.
* **Diagnostics:** `curl localhost:18002/health` — detailed JSON verdict without probe semantics.
  Its runtime block identifies the selected coordinator/publisher, poller lease state, relay result,
  outbox `pending`/`published`/`poison` counts, and remaining horizontal-scaling blockers.
* **Polling a run:** `curl -H "Authorization: Bearer $CONTROL_PLANE_TOKEN" localhost:18002/retrain/<id>` returns `{flow_run_id, state_type, state_name, is_terminal}`. `is_terminal` is true for `COMPLETED / FAILED / CANCELLED / CRASHED`.
* **Logs:** `make control-plane-logs` (or via Loki when the monitoring stack is up — labels: `compose_service="control-plane"`).

## Prometheus Metrics (Phase 13)

The control plane exposes a Prometheus-compatible `/metrics` endpoint that the existing monitoring stack scrapes automatically. No authentication required — this is consistent with the Ray Serve `/metrics` endpoint and is safe because the control plane is only reachable inside the Docker network.

### Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `examlops_approvals_pending` | Gauge | — | Current count of rows with `status = 'pending'`; updated on every create/approve/reject |
| `examlops_approval_events_total` | Counter | `model_id`, `action` | Cumulative approval lifecycle events; `action` ∈ `{created, approved, rejected}` |
| `examlops_approval_age_oldest_seconds` | Gauge | — | Age in seconds of the oldest pending approval; 0 when no pending approvals; computed on each `/metrics` scrape |

### Scrape configuration

The `control_plane` job is already configured in `platform/infra/docker-compose/prometheus.yml`:

```yaml
- job_name: control_plane
  static_configs:
    - targets: ['control-plane:8002']
  metrics_path: /metrics
```

Prometheus scrapes it every 15 seconds (global `scrape_interval`).

### Grafana dashboard

The **ExaMLOps — Approval Gate** dashboard (`platform/infra/docker-compose/grafana/provisioning/dashboards/examlops_approvals.json`) is provisioned automatically when Grafana starts. It contains three panels:

| Panel | Type | PromQL |
|---|---|---|
| Pending Approvals | Stat (green = 0, red ≥ 1) | `examlops_approvals_pending` |
| Oldest Pending Age (min) | Stat (yellow > 30 min, red > 60 min) | `examlops_approval_age_oldest_seconds / 60` |
| Approval Events Rate | Time series | `rate(examlops_approval_events_total[5m])` |

### Key alert threshold

`examlops_approval_age_oldest_seconds > 3600` — a model change has been waiting for approval for more than one hour. Configure this in Grafana Alerting → Alert rules, pointing to the `control_plane` contact point of your choice.

### Error handling

- If the SQLite read fails at scrape time, the error is logged, the age gauge is set to 0, and `generate_latest()` is still returned — the scrape never returns HTTP 500.
- `prometheus-client` import errors at startup are non-fatal: `/metrics` returns 503 with a plain message.
