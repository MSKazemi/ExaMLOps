# Control Plane

`platform/services/control_plane/app.py` is a thin FastAPI gateway in front of Prefect. It lets clients (and the synthetic dataplane simulator) request a retraining run for a registered model **without holding Prefect credentials themselves**. Phase 4 of the master rollout.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness + Prefect URL + auth state + registered models + pending approval count |
| GET | `/models` | none | List `model_name → datasets` known to the auto-discovery registry |
| POST | `/retrain` | **Bearer** | Validate + schedule a Prefect flow run |
| GET | `/retrain/{flow_run_id}` | none | Poll Prefect for the run state |
| POST | `/api/changes` | **Bearer** | CI webhook — record changed model IDs as pending approvals (no training yet) |
| GET | `/approvals` | none | List approvals; filter by `?status=pending\|approved\|rejected` |
| POST | `/approve/{model_id}` | **Bearer** | Approve a pending change — fires Prefect training run immediately |
| POST | `/reject/{model_id}` | **Bearer** | Reject a pending change with optional `{"reason": "..."}` body |
| POST | `/webhooks/modelzoo/gitlab` | token header | GitLab push webhook — mark models stale, optionally auto-retrain |
| POST | `/webhooks/modelzoo/github` | HMAC header | GitHub push webhook — same semantics as GitLab |
| GET | `/modelzoo/status` | none | Per-model freshness: `current` / `stale` / `unknown` |
| GET | `/modelzoo/events` | none | Recent push event history (`?limit=N`) |
| POST | `/modelzoo/sync` | none | Manually trigger one GitLab poll cycle |
| GET | `/modelzoo/config` | none | Show runtime ModelZoo config |
| PUT | `/modelzoo/config` | **Bearer** | Update runtime config (takes effect immediately) |
| GET | `/metrics` | none | Prometheus text-format metrics for the approval gate (Phase 13) |

## POST /retrain

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
3. `Authorization: Bearer <token>` must match `CONTROL_PLANE_TOKEN`.

Failure modes:

| HTTP | When |
|---|---|
| 400 | Unknown `model_name` or unsupported `dataset_name` |
| 401 | Missing `Authorization` header |
| 403 | Wrong bearer token |
| 503 | `CONTROL_PLANE_TOKEN` env var is unset (fail-closed, no silent allow) |
| 502 | Prefect API unreachable / 5xx |

## Approval Gate (Phase 11)

When a developer pushes to the modelzoo repo on GitHub, CI runs `ci/notify_model_changes.py`, which diffs the commits, extracts changed `model_id` values, and POSTs to `POST /api/changes`. The Control Plane stores these as `pending` rows in a SQLite database — **no training runs yet**.

A sysadmin then approves or rejects each change:

```bash
# List pending approvals
exa approvals list
curl http://localhost:18002/approvals?status=pending

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
| `requested_at` | ISO 8601 timestamp of the CI push |
| `resolved_at` | ISO 8601 timestamp of approve/reject |

The dashboard Approvals page (admin-only) shows a badge with the pending count and lets sysadmins approve or reject with one click.

### CI integration

The GitHub Actions `examlops` job calls `ci/notify_model_changes.py` on every push to `main`. The script:
1. Diffs `before..after` commits.
2. Finds changed files under `modelzoo/modelzoo/models/tasks/`, `pipelines/model_configs/`, and `pipelines/models/`.
3. Extracts `model_id` values via regex.
4. POSTs to `POST /api/changes` with bearer auth.
5. **Fails silently** on connection error — never blocks CI.

Required GitHub secrets: `CONTROL_PLANE_URL` and `CONTROL_PLANE_TOKEN`.

## Auth model

`CONTROL_PLANE_TOKEN` is the shared secret. Set it in the docker-compose `.env` file (or pass it via env on a real deployment) and in any client / dataplane that calls `POST /retrain`. Read-only endpoints (`/health`, `/models`, `/retrain/{flow_run_id}`) require no auth.

If the env var is missing, `POST /retrain` always returns 503 — the service refuses to silently allow unauthenticated writes.

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

`GET /models` and `POST /retrain` validation both rely on knowing which models (and their datasets) are registered. The control plane reads this directly from `pipelines/models/*.yaml` — it does **not** import `pipeline_generator.py` or any modelzoo Python code.

This approach (`_load_registry()` in `app.py`) avoids pulling PyTorch and the rest of the modelzoo's training dependencies into the lightweight control-plane container. The registry is re-read on every call (no in-process cache), so adding a new YAML file takes effect immediately on the next request without restarting the container.

`model_meta.py` follows the same pattern: it scans YAML files for metadata (task type, schema, promotion rules, serving aliases) and locates the model's source directory by text-scanning the modelzoo for `class <ModelClass>` — never importing the class itself.

## ModelZoo Integration (Phase 12)

The control plane is the authoritative hub for ModelZoo repository freshness tracking. It receives push events from GitLab/GitHub webhooks and from a background poller, records them in SQLite, and exposes freshness state via REST endpoints. The dashboard and `exa` CLI both consume these endpoints.

### How it works

1. **Push event arrives** — via `POST /webhooks/modelzoo/gitlab` (or `/github`), or discovered by the background poller querying the GitLab API.
2. **Event recorded** — a row is inserted into `modelzoo_events` (commit SHA, branch, pushed_by, timestamp, source).
3. **All models marked stale** — every model in the auto-discovery registry gets an upserted row in `model_freshness` with `is_stale=1` and the current timestamp as `stale_since`.
4. **Optional auto-retrain** — if `_modelzoo_config["auto_retrain"]` is true, `POST /retrain` fires for each model with its first supported dataset.
5. **Freshness cleared** — when a model is approved and trained, its `model_freshness` row is updated with `is_stale=0` and `last_retrain_commit` set to the latest ModelZoo commit.

### Webhook registration

**GitLab** — Settings → Webhooks → add URL `http://<control-plane>:18002/webhooks/modelzoo/gitlab`, select *Push events*, set secret token to `MODELZOO_WEBHOOK_SECRET`.

**GitHub** — Settings → Webhooks → add URL `http://<control-plane>:18002/webhooks/modelzoo/github`, content type `application/json`, select *Push events*, set secret to `MODELZOO_WEBHOOK_SECRET`.

The dashboard Config page (ModelZoo Integration section) shows the pre-filled webhook URL derived from the live control plane address.

### Background poller

The poller runs as a daemon thread inside the control plane process. At startup `_start_poller()` is called (a FastAPI startup hook). It loops with `time.sleep(_modelzoo_config["poll_interval_seconds"])` — so interval changes via `PUT /modelzoo/config` take effect on the next sleep boundary without restarting the service.

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

### SQLite tables

Two tables are created on first startup alongside the existing `pending_approvals` table:

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
| `last_retrain_commit` | TEXT | SHA at last successful retrain |
| `is_stale` | INTEGER | 1 = stale, 0 = current |
| `stale_since` | TEXT | ISO 8601 when stale flag was set |
| `retrain_triggered_at` | TEXT | ISO 8601 of last auto-retrain trigger |

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_PORT` | `8002` | HTTP port |
| `CONTROL_PLANE_TOKEN` | unset (required) | Bearer token for `POST /retrain` |
| `PREFECT_API_URL` | `http://localhost:4200/api` | Prefect server endpoint |
| `PREFECT_DEPLOYMENT_NAME` | `examlops_scheduled_training/nightly` | Deployment slug `POST /retrain` schedules (must be `flow_name/deployment_name`) |
| `CONTROL_PLANE_URL` | `http://control-plane:8002` | Set on the dataplane simulator so it can forward |
| `CONTROL_PLANE_DB` | `/data/approvals.db` | SQLite file path for the Phase 11 pending approval store; falls back to `./approvals.db` if `/data/` is not writable |
| `MODELZOO_WEBHOOK_SECRET` | unset | Shared secret for webhook HMAC/token verification |
| `MODELZOO_AUTO_RETRAIN` | `false` | Trigger auto-retrain on every push event |
| `MODELZOO_POLL_SECONDS` | `300` | GitLab poller interval (seconds); `0` disables |
| `MODELZOO_WATCH_BRANCH` | `main` | Branch watched by webhooks and poller |
| `GITLAB_PROJECT_ID` | unset | GitLab project ID or namespace/path for the poller |
| `GITLAB_TOKEN` | unset | GitLab Personal/Project Access Token (`read_repository` scope) for the poller |

## Operations

* **Health probe:** `curl localhost:18002/health` — returns `{status, prefect_api_url, deployment, auth_configured, models}`. The compose health-check uses the same endpoint.
* **Polling a run:** `curl localhost:18002/retrain/<id>` returns `{flow_run_id, state_type, state_name, is_terminal}`. `is_terminal` is true for `COMPLETED / FAILED / CANCELLED / CRASHED`.
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
