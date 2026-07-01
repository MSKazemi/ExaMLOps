# API Reference

## Ray Serve Inference API (port 18001)

Interactive docs at **http://localhost:18001/docs** (Swagger) and **/redoc**.

### `GET /health`

Liveness + readiness. No authentication required.

**Response 200:**
```json
{
  "status": "ok",
  "models_loaded": 2,
  "models": {
    "JPCP": {"version": "3", "run_id": "abc123", "status": "ok"},
    "MACK": {"version": "1", "run_id": "def456", "status": "ok"}
  }
}
```

`status` is `"degraded"` when `models_loaded == 0`.

---

### `GET /models`

List all currently loaded models.

**Response 200:**
```json
[
  {"model_name": "JPCP", "model_version": "3", "run_id": "abc123", "status": "ok"},
  {"model_name": "MACK", "model_version": "1", "run_id": "def456", "status": "ok"}
]
```

---

### `POST /predict/{model_name}`

Run inference. The `features` dict keys must match the column names the model was trained on.

**Request:**
```json
{"features": {"feature_0": 1.2, "feature_1": 0.8, "feature_2": 3.4}}
```

**Response 200:**
```json
{
  "model_name": "JPCP",
  "model_version": "3",
  "run_id": "abc123",
  "prediction": 142.7
}
```

**Errors:**

| Code | Body | Reason |
|---|---|---|
| 404 | `{"detail": "Model 'X' is not loaded..."}` | No `@Production` version, or name typo |
| 500 | `{"detail": "..."}` | Feature shape mismatch or model exception |

---

### `POST /reload`

Hot-reload: re-scan MLflow for `@Production` models and refresh in-place.

**Response 200:**
```json
{"reloaded": ["JPCP", "MACK"], "count": 2}
```

---

## Dashboard API (port 8099)

All routes except `/api/health` require `Authorization: Bearer <token>`.

Default token: `changeme` (set `DASHBOARD_TOKEN` to override).

### `GET /api/health`

Aggregate health of all services. No authentication required.

**Response 200:**
```json
{
  "status": "ok",
  "checked_at": "2026-04-30T12:00:00+00:00",
  "services": {
    "mlflow":     {"status": "ok",      "url": "http://localhost:5000"},
    "prefect":    {"status": "ok",      "url": "http://localhost:4200"},
    "ray_serve":  {"status": "degraded","url": "http://localhost:8265"},
    "prometheus": {"status": "ok",      "url": "http://localhost:9090"},
    "grafana":    {"status": "ok",      "url": "http://localhost:3000"}
  }
}
```

`status` per service: `"ok"`, `"degraded"` (non-2xx response), `"down"` (no connection).

---

### `GET /api/config`

Get stored dashboard configuration.

**Headers:** `Authorization: Bearer <token>`

**Response 200:**
```json
{
  "mlflow_url": "http://localhost:5000",
  "grafana_url": "http://localhost:3000"
}
```

---

### `PUT /api/config`

Update one or more config keys.

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{"mlflow_url": "http://192.168.1.10:5000"}
```

**Response 200:** updated config object.

---

### `GET /api/proxy/{service}/{path}`

Transparent proxy to internal services. Rewrites the response so links work from the browser.

Supported services: `mlflow`, `prefect`, `ray`, `ray_dashboard`, `prometheus`, `grafana`, `control_plane`.

**Headers:** `Authorization: Bearer <token>`

---

### `GET /api/docs/tree`

Returns the documentation file tree.

**Headers:** `Authorization: Bearer <token>`

**Response 200:**
```json
[
  {
    "key": "overview",
    "title": "Overview",
    "files": [
      {"path": "README.md", "title": "Project Overview"}
    ]
  },
  ...
]
```

---

### `GET /api/docs/content?path={path}`

Returns raw markdown content of a documentation file. `path` is relative to the project root and must be within the allowed set.

**Headers:** `Authorization: Bearer <token>`

**Response 200:** `text/plain` — raw markdown content.

**Errors:**

| Code | Reason |
|---|---|
| 400 | Path traversal attempt or non-markdown file |
| 404 | File not found |

---

## Control Plane API (port 18002)

Endpoints for retraining, the Phase 11 approval gate, and Phase 12 ModelZoo integration. See [Control Plane guide](../guides/control-plane.md) for the full workflow.

### `GET /health`

**Response 200:**
```json
{
  "status": "ok",
  "prefect_api_url": "http://localhost:4200/api",
  "auth_configured": true,
  "models": ["JPCP", "MACK", "MCBound"],
  "pending_approvals": 1
}
```

---

### `POST /retrain`

**Auth:** `Authorization: Bearer <CONTROL_PLANE_TOKEN>`

**Request:**
```json
{
  "model_name": "JPCP",
  "dataset_name": "PM100Dataset",
  "is_dummy": true,
  "backend_name": "minio"
}
```

**Response 200:**
```json
{
  "flow_run_id": "abc123",
  "deployment": "examlops_scheduled_training/nightly",
  "status_url": "/retrain/abc123"
}
```

---

### `POST /api/changes` (Phase 11)

CI webhook — records changed model IDs as pending approvals. Does not fire training.

**Auth:** `Authorization: Bearer <CONTROL_PLANE_TOKEN>`

**Request:**
```json
{
  "model_ids": ["JPCP", "MACK"],
  "commit_sha": "abc123",
  "commit_msg": "feat: improve JPCP features",
  "changed_files": ["pipelines/model_configs/jpcp_config.py"]
}
```

**Response 200:**
```json
{"created": 2}
```

---

### `GET /approvals` (Phase 11)

List approval records. Filter with `?status=pending|approved|rejected`.

**Response 200:**
```json
[
  {
    "id": 1,
    "model_id": "JPCP",
    "status": "pending",
    "commit_sha": "abc123",
    "commit_msg": "feat: improve JPCP features",
    "changed_files": ["pipelines/model_configs/jpcp_config.py"],
    "requested_at": "2026-05-21T10:00:00Z",
    "resolved_at": null,
    "prefect_run_id": null,
    "reject_reason": null
  }
]
```

---

### `POST /approve/{model_id}` (Phase 11)

Approve a pending change — fires Prefect training immediately.

**Auth:** `Authorization: Bearer <CONTROL_PLANE_TOKEN>`

**Response 200:**
```json
{"model_id": "JPCP", "status": "approved", "flow_run_id": "xyz789"}
```

**Errors:**

| Code | Reason |
|---|---|
| 404 | No pending approval found for `model_id` |
| 502 | Prefect unreachable |

---

### `POST /reject/{model_id}` (Phase 11)

Reject a pending change. No training runs.

**Auth:** `Authorization: Bearer <CONTROL_PLANE_TOKEN>`

**Request (optional):**
```json
{"reason": "needs data review"}
```

**Response 200:**
```json
{"model_id": "JPCP", "status": "rejected"}
```

---

## ModelZoo Integration Endpoints (Phase 12)

### `POST /webhooks/modelzoo/gitlab`

Receive a GitLab push event. Marks all registered models stale, records a push event, and optionally triggers auto-retrain.

**Auth:** `X-Gitlab-Token: <MODELZOO_WEBHOOK_SECRET>` header (plain equality; optional if secret not configured)

**Request body:** GitLab push webhook payload (JSON)

**Response 200:**
```json
{
  "event_id": 5,
  "models_marked_stale": 3,
  "retrain_triggered": false
}
```

Returns `{"skipped": true, "reason": "..."}` if the push is to a non-watched branch or carries no commit SHA.

---

### `POST /webhooks/modelzoo/github`

Receive a GitHub push event. Same semantics as the GitLab endpoint.

**Auth:** `X-Hub-Signature-256: sha256=<HMAC-SHA256>` header (HMAC verification; optional if secret not configured)

**Request body:** GitHub push webhook payload (JSON)

**Response 200:** same shape as GitLab webhook.

---

### `GET /modelzoo/status`

Per-model freshness snapshot.

**Response 200:**
```json
{
  "models": [
    {
      "model_id": "JPCP",
      "status": "stale",
      "latest_modelzoo_commit": "abc12345",
      "last_retrain_commit": "def67890",
      "stale_since": "2026-05-21T10:00:00",
      "retrain_triggered_at": null
    },
    {
      "model_id": "MACK",
      "status": "current",
      "latest_modelzoo_commit": "abc12345",
      "last_retrain_commit": "abc12345",
      "stale_since": null,
      "retrain_triggered_at": null
    }
  ],
  "last_event": {
    "commit_sha": "abc12345",
    "timestamp": "2026-05-21T10:00:00",
    "source": "webhook"
  }
}
```

`status` values: `"current"` (latest modelzoo commit matches last retrain commit), `"stale"` (new push not yet retrained), `"unknown"` (no freshness data recorded yet).

---

### `GET /modelzoo/events`

Recent push event history. Accepts `?limit=N` (default 20).

**Response 200:**
```json
[
  {
    "id": 5,
    "commit_sha": "abc12345",
    "branch": "main",
    "pushed_by": "alice",
    "timestamp": "2026-05-21T10:00:00",
    "source": "webhook"
  }
]
```

`source` is `"webhook"` (GitLab or GitHub push) or `"poll"` (background poller).

---

### `POST /modelzoo/sync`

Manually trigger one GitLab poll cycle. Useful for testing poller connectivity or forcing a freshness check without waiting for the next scheduled interval.

**Response 200:**
```json
{"new_commit": true, "commit_sha": "abc12345", "models_marked_stale": 3}
```

or `{"new_commit": false}` when already up-to-date.

---

### `GET /modelzoo/config`

Show current runtime ModelZoo integration config.

**Response 200:**
```json
{
  "auto_retrain": false,
  "poll_interval_seconds": 300,
  "watch_branch": "main"
}
```

---

### `PUT /modelzoo/config`

Update runtime config. Changes take effect immediately (poller sleep and auto-retrain flag both read from this config at runtime — no restart needed).

**Auth:** `Authorization: Bearer <CONTROL_PLANE_TOKEN>`

**Request:**
```json
{"auto_retrain": true, "poll_interval_seconds": 120}
```

All fields are optional. Unknown keys are ignored.

**Response 200:** updated config object.

---

## Prometheus Metrics Endpoint (Phase 13)

### `GET /metrics`

Prometheus text-format metrics for the approval gate. **No authentication required.** The endpoint is safe without auth because it's only reachable inside the Docker network.

**Response 200** (`Content-Type: text/plain; version=0.0.4`):
```text
# HELP examlops_approvals_pending Current number of pending model change approvals
# TYPE examlops_approvals_pending gauge
examlops_approvals_pending 1.0
# HELP examlops_approval_events_total Cumulative approval lifecycle events
# TYPE examlops_approval_events_total counter
examlops_approval_events_total{action="created",model_id="JPCP"} 1.0
examlops_approval_events_total{action="approved",model_id="JPCP"} 0.0
# HELP examlops_approval_age_oldest_seconds Age in seconds of the oldest pending approval; 0 when none pending
# TYPE examlops_approval_age_oldest_seconds gauge
examlops_approval_age_oldest_seconds 342.8
```

The `examlops_approval_age_oldest_seconds` value is recomputed on each scrape by querying `SELECT MIN(requested_at) FROM pending_approvals WHERE status = 'pending'`. It returns `0.0` when no pending approvals exist.
