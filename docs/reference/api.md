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

All routes except `/api/health`, `POST /api/auth/login` and the SSO routes below require an
authenticated caller, in one of three ways (ADR 0120):

- the **SSO session cookie** set by the organisation sign-in (`__Host-examlops_session`, HttpOnly).
  A cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE` must also prove same-origin
  (`Sec-Fetch-Site: same-origin`, a matching `Origin`, or the header `X-ExaMLOps-CSRF: 1`);
- `Authorization: Bearer <session token>` from `POST /api/auth/login` (local break-glass passwords,
  unless `DASHBOARD_LOCAL_LOGIN=false`);
- `Authorization: Bearer <access token>` issued by a data center's IdP listed in the trust file.

### Organisation sign-in (ADR 0120)

| Route | Purpose |
|---|---|
| `GET /api/auth/sso/providers` | `{"providers": [{name, display_name, login_url, step_up}], "local_login": bool}` — unauthenticated, for the login page |
| `GET /api/auth/sso/{provider}/login?return_to=/path&step_up=true` | 302 to the center's IdP (authorization code + PKCE S256); `step_up` asks for the provider's `acr_values` with `max_age=0` |
| `GET /api/auth/sso/callback` | the registered redirect URI: checks `state` and RFC 9207 `iss`, exchanges the code, verifies the ID token, sets the session cookie, 302 to `return_to`; failures 302 to `/?sso_error=<reason>` |
| `POST /api/auth/sso/logout` | clears the session; returns `{"end_session_url": …}` for RP-initiated logout at the IdP |
| `GET /api/auth/me` | now also returns `sub`, `name`, `idp`, `auth_method` (`password`/`sso`/`idp-bearer`) and `acr` |

### SCIM 2.0 provisioning (ADR 0132)

`/api/scim/v2` is the provisioning endpoint for the centers' IdPs, not a dashboard-session API.
Each call authenticates with the center's own SCIM bearer (`provisioning.token_ref` in its trust
entry) and is confined to that center's accounts. Responses are `application/scim+json`, and errors
follow RFC 7644 §3.12.

| Route | Purpose |
|---|---|
| `GET /api/scim/v2/ServiceProviderConfig` · `/ResourceTypes` · `/Schemas` | discovery |
| `GET /api/scim/v2/Users?filter=userName eq "x"&startIndex=1&count=100` | list / find (`eq` on `userName`, `externalId`, `id`, `emails`) |
| `POST /api/scim/v2/Users` | provision (`409 uniqueness` if it exists; a deleted account is revived) |
| `GET` · `PUT` · `PATCH` `/api/scim/v2/Users/{id}` | read, replace, update (`active: false` deactivates) |
| `DELETE /api/scim/v2/Users/{id}` | deprovision (tombstoned: refused forever, `404` afterwards) |

A step-up action the session does not satisfy answers `401` with
`WWW-Authenticate: Bearer error="insufficient_user_authentication", acr_values="…", max_age=N`
(RFC 9470). Guide: [Identity federation](../guides/identity-federation.md).

### `GET /api/health`

Aggregate health of all services. No authentication required.

**Response 200:**
```json
{
  "status": "ok",
  "checked_at": "2026-04-30T12:00:00+00:00",
  "services": {
    "mlflow":     {"status": "ok",       "url": "http://localhost:15000"},
    "prefect":    {"status": "ok",       "url": "http://localhost:14200"},
    "ray_serve":  {"status": "degraded", "url": "http://localhost:18001"},
    "prometheus": {"status": "ok",       "url": "http://localhost:19090"},
    "grafana":    {"status": "ok",       "url": "http://localhost:13000"},
    "slurm":      {"status": "unknown",  "url": "",
                   "note": "EXAMLOPS_SLURM_MODE=mock — training runs inline, so there is no scheduler to probe."}
  },
  "unmeasured": ["seanerbus_sim", "slurm"]
}
```

`status` per service: `"ok"`, `"degraded"` (non-2xx response), `"down"` (no connection), and
`"unknown"` — *nothing was probed*, so no verdict is claimed. An `unknown` entry always carries a
`note` saying why, and every such key is listed in the top-level `unmeasured` array.

**`unmeasured` entries do not move the top-level `status`.** Two services are reported for
completeness and never probed: `slurm` (the scheduler runs off-cluster, or in `mock` mode is not
involved at all — use `exa hpc queue` / `exa hpc capacity` instead) and `seanerbus_sim` (the bridge
does not report its bus connection, so only the bridge's own HTTP endpoint is measured, under the
`seanerbus` key). Before this, `slurm` answered `ok` in mock mode and `down` in every other mode,
which held the whole payload at `degraded` for the entire life of a real-scheduler deployment.

The URLs above are the **host** ports the compose stack publishes (`15000:5000`, `14200:4200`, …).
Inside the compose network the dashboard is given the service-name addresses instead.

---

### `GET /api/v1/overview` (BFF, F8)

View-shaped platform overview composed by the **Backend-for-Frontend** layer. Requires the
`viewer` role. Sources are fetched concurrently with a per-source timeout; any source that fails
or times out is **omitted** and listed under `_partial` (the response is still `200` — a partial
view beats an error page). See [dashboard architecture](../dashboard/architecture.md#backend-for-frontend-bff-layer-f8).

**Response 200:**
```json
{
  "meta":    {"service": "dashboard-bff", "api": "v1"},
  "traffic": {"models_with_rules": 3},
  "drift":   {"models_tracked": 5},
  "audit":   {"total_events": 128}
}
```

**Response 200 (partial — the drift source was slow/down):**
```json
{
  "meta":    {"service": "dashboard-bff", "api": "v1"},
  "traffic": {"models_with_rules": 3},
  "audit":   {"total_events": 128},
  "_partial": ["drift"]
}
```

---

### `GET /api/v1/stream` (BFF realtime, F8)

Multiplexed **Server-Sent-Events** stream of live platform events. Requires the `viewer` role.
`Content-Type: text/event-stream`.

**Query:** `channels` — comma-separated channel globs (default `*` = all). Namespaces:
`job`, `drift`, `alert`, `deploy`, `approval`, `event`. A bare namespace (`job`) expands to `job.*`.

Events are tenant-filtered (a client never sees another tenant's events) and backpressured (under a
flood the oldest queued event is dropped; each frame carries a `_dropped` count).

**Stream:**
```
event: hello
data: {"channels": ["job.*", "drift.*"]}

event: job.started
data: {"model": "JPCP", "run_id": "abc123", "_dropped": 0}

: keep-alive
```

Clients **should** fall back to polling the relevant `/api/v1/*` view endpoints when the stream is
unavailable, and reconnect when it recovers.

---

### `GET /api/v1/mlops/registry` (MLOps console, F9)

Registry grid rows for the MLOps console. Requires the `viewer` role. Composed through the F8 BFF
substrate, so a slow/down source yields a `_partial`-tagged payload instead of a 500.

**Response:**
```json
{
  "registry": {
    "count": 2,
    "rows": [
      {"name": "JPCP", "mlflowName": "jpcp", "version": 18, "stage": "Production",
       "health": "ok", "freshness": "2026-07-02T12:00:00", "governed": true},
      {"name": "DEMOAD", "mlflowName": "demoad", "version": null, "stage": "Staging",
       "health": "warn", "freshness": "2026-07-01T09:00:00", "governed": false}
    ]
  }
}
```

`health` is a colour-blind-safe token (`ok` / `warn` / `unknown`); `governed` is `true` when an
enabled promotion policy exists. `name`/`mlflowName` carry the central uppercase↔lowercase mapping.

### `GET /api/v1/mlops/model/{name}` (MLOps console, F9)

Model-detail-2.0 tabs for one model (case-insensitive `name`). Requires `viewer`.

**Response:** `{"detail": {"name", "mlflowName", "cost": {...}, "drift": {...}, "traffic": {...}, "promotion": {...}}}`
— `cost` (runs/gpu_hours/cost_usd), `drift` (samples/mean_prediction/latest), `traffic`
(configured/rules/updated_at), and the embedded `promotion` gate.

### `GET /api/v1/mlops/promotion/{name}` (MLOps console, F9)

Guided-promotion gate for one model. Requires `viewer`.

**Response:**
```json
{
  "promotion": {
    "model": "DEMOAD", "mlflowName": "demoad",
    "policy": {"allow": false, "reasons": ["no promotion policy configured"]},
    "eval": {"pass": false, "metrics": {}},
    "approval": {"required": true, "state": "pending"},
    "allowed": false
  }
}
```

A promotion is `allowed` only when an **enabled** promotion policy exists; otherwise it is denied
with an explicit `reasons` list (F9 R4). The phase-11 approval step is always flagged as required.

---

### `GET /api/v1/facility/overview` (Facility console, F6)

Scheduler-neutral HPC facility KPIs. Requires `viewer`. BFF-composed (partial-failure safe).
Optional `?cluster=<scheduler>` rescopes to one cluster (multi-cluster switcher, F6 R6).

**Response:**
```json
{
  "facility": {
    "nodesAllocated": 6, "gpusAllocated": 12, "jobsRunning": 2, "queueDepth": 2,
    "clusters": ["flux", "slurm"],
    "partitions": [
      {"name": "flux",  "running": 1, "queued": 0, "gpusAllocated": 8},
      {"name": "slurm", "running": 1, "queued": 2, "gpusAllocated": 4}
    ]
  }
}
```

Allocation sums the node/GPU asks of running jobs; queue depth counts waiting jobs. A missing
`hpc_jobs` table / empty DB degrades to zeros (F6 R7), never an error.

### `GET /api/v1/facility/queue` (Facility console, F6)

Waiting jobs, longest-wait first (`queue_seconds` proxies priority/backfill). Requires `viewer`.
Optional `?cluster=`.

**Response:** `{"queue": {"count": 2, "jobs": [{"id", "cluster", "model", "dataset", "state", "waitSec", "nodes", "gpus", "submitTime"}]}}`

### `GET /api/v1/facility/job/{job_id}` (Facility console, F6)

Per-job detail. Requires `viewer`. `404` when the job is unknown.

**Response:** `{"job": {"id", "cluster", "model", "dataset", "state", "resources": {"nodes","gpus","cpus"}, "timing": {"submit","start","end","queueSeconds","runSeconds"}, "exitCode", "flowRunId", "mlflowRunId"}}`
— `mlflowRunId` is the cost link into `model_costs` / MLflow.

---

### `GET /api/v1/flags` (Feature flags, F25)

Server-evaluated flag **decisions** for the caller's context (tenant/role/percentage). Requires
`viewer`. Returns `{"flags": {"mlopsConsole": true, "incidentTimeline": false, …}}` — booleans, not
rules. Percentage rollouts are deterministic per subject (admins bypass gating).

### `GET /api/v1/flags/admin` (Feature flags, F25)

Flag definitions + overrides + effective state for the admin UI. Requires **admin**.

**Response:** `{"flags": [{"name","description","default","override","effective","targeting":{"tenants","roles","percentage"},"tags"}], "count": N}`

### `POST /api/v1/flags/{name}` (Feature flags, F25)

Set an admin on/off override. Requires **admin**. Body `{"enabled": false}`. Persists to
`feature_flag_overrides`, audits to `platform_db` (D4), and publishes `event.flag_changed` on the F8
channel. Returns `{"updated": true, "name": "...", "enabled": false}` (`updated: false` for an
unknown flag).

---

### Collaboration (F22)

All viewer-gated, tenant-scoped (F15), sanitized (F16), and audited (`source=dashboard-collab`, D4).

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/collab/{type}/{id}/comments` | List comments on an entity (tenant-scoped) |
| `POST /api/v1/collab/{type}/{id}/comments` | Add a comment `{"body": "…@user…"}` — sanitized; @-mentions publish `event.mention` (F12) |
| `GET /api/v1/collab/{type}/{id}/activity` | Merged activity trail (comments + entity audit events) |
| `POST /api/v1/collab/snapshot` | Create a shareable frozen-view snapshot `{"view": {...}, "ttl_hours": 168}` → `{"token","expires_at"}` |
| `GET /api/v1/collab/snapshot/{token}` | Resolve a snapshot (read-only, expiry-checked) → `{"found": true, "view", "read_only": true}` or `{"found": false}` |

A comment response is `{"id","author","body","mentions":[…],"created_at"}` (the body is stored sanitized).
Snapshot tokens are scoped, read-only, and expiring — there is no write path and no cross-tenant read.

---

### `POST /api/v1/copilot/ask` (Embedded copilot, F11)

Ask the grounded copilot a question; it proxies the existing Skipper agent bridge through a read-only
agent graph and returns an answer plus **propose-only** `exa` actions (never executed) and an agent
trace. Requires `viewer`. The backend attempts to audit each query (`source=dashboard-copilot`); an
audit-store failure does not fail the response.

**Body:** `{"question": "why is jpcp drifting?", "context": {"page": "/models/jpcp", "entity": {...}, "filters": {...}}}`
— page context is treated as **untrusted** data server-side (R6). The server derives the conversation
ID from the signed login token; clients cannot choose another user's checkpoint key.

**Response:** `{"answer": "…", "hitl_required": false, "proposals": [{"command": "exa retrain jpcp …", "requiresApproval": true}], "trace": [{"kind","name","detail"}]}`.
When the agent is unreachable the same shape is returned with `"_partial": ["agent"]` (never a 500).
Mutating proposals carry `requiresApproval: true` and must route through the normal approval flow — there
is no execution endpoint.

---

### `GET /api/v1/alerts` (Alerting, F12)

Unified alert inbox derived from drift / budget / eval signals. Requires `viewer`. BFF-composed.

**Response:**
```json
{
  "inbox": {
    "count": 3,
    "counts": {"critical": 1, "error": 1, "warn": 1},
    "alerts": [
      {"id":"drift:jpcp","source":"drift","severity":"critical","state":"firing",
       "title":"Prediction drift on jpcp (8.0σ from baseline)","labels":{"model":"jpcp","zscore":"8.00"}}
    ]
  }
}
```

Alerts are sorted most-severe first. Sources: `drift` (z-score vs baseline), `budget` (spend > budget),
`eval` (a failed metric in the latest run).

### `POST /api/v1/alerts/{alert_id}/ack` (Alerting, F12)

Acknowledge an alert. Requires `viewer`. Audits the ack to `platform_db` (D4) and publishes
`alert.acked` on the F8 realtime channel. Returns `{"acked": true, "audited": true}`.

---

### `GET /api/v1/llmops/overview` (LLMOps console, F10)

LLM endpoint registry + continuous-eval scores. Requires `viewer`. BFF-composed (partial-failure
safe); unavailable backends degrade to empty sections.

**Response:**
```json
{
  "endpoints": {"rows": [{"model":"llama3","engine":"vllm","hfModelId":"meta-llama/Llama-3-8B",
                "maxModelLen":8192,"tensorParallel":2,"dtype":"bfloat16","enabled":true}], "count": 1},
  "evals": {"models": [{"model":"llama3","suite":"mmlu","status":"complete",
            "metrics":[{"metric":"accuracy","value":0.82,"baseline":0.80,"passed":true}],
            "passRate":0.5}], "count": 1}
}
```

`evals` reflects only each model's **latest** eval run. `passRate` is the fraction of metrics that
passed (null when a run has no metric rows).

---

### `GET /api/v1/governance/overview` (Governance & compliance, F14)

Governance overview: NIST posture + EU-AI-Act compliance + model-card coverage + audit integrity.
Requires `viewer`. BFF-composed (partial-failure safe). Reports evidence coverage, not certification.

**Response:**
```json
{
  "posture": {"controls": [{"control":"MANAGE-4.1","function":"Manage","title":"Change approval logged",
              "status":"satisfied","evidence":["1 approval audit events"]}], "satisfied": 3, "total": 4},
  "compliance": {"rows": [{"model":"jpcp","version":18,"riskClass":"high","technicalFile":true,"provenance":true}],
                 "count": 1},
  "cards": {"withCard":["jpcp"], "withoutCard":["demo"], "coverage": 0.5, "total": 2},
  "audit": {"count": 2, "headDigest": "9f2c…", "verified": true, "entries": [{"seq":1,"hash":"…","prevHash":"genesis"}]}
}
```

`status` is `satisfied` / `partial` / `gap` (honest, no false green). `audit.headDigest` is a rolling
SHA-256 hash-chain anchor — an external copy detects tampering of any past event.

---

### `GET /api/v1/selfobs/status` (Self-observability, F24)

In-app status page payload: dependency health + dashboard self-metrics. Requires `viewer`.

**Response:**
```json
{
  "status": "up",
  "dependencies": [
    {"name": "bff", "status": "up"},
    {"name": "platform_db", "status": "up", "latencyMs": 0.4}
  ],
  "metrics": {"requests": 128, "errors": 0, "clientErrors": 3, "rateLimitHits": 0,
              "latencyMs": {"count": 128, "p50": 2.1, "p95": 18.7}}
}
```

### `POST /api/v1/selfobs/action` (Self-observability, F24)

Audit a UI action to `platform_db.audit_events` (D4). Requires `viewer`. Body:
`{"action": "open_page", "target": "/mlops", "details": ""}` → `{"audited": true}` (false if the
audit table is absent). The actor is the caller's role; details are PII-scrubbed client-side first.

---

### `GET /api/v1/finops/overview` (FinOps & Green-AI, F13)

Cost + budget + carbon + unit-economics overview. Requires `viewer`. BFF-composed (partial-failure
safe) over `model_costs` / `project_budgets` / `carbon_records`.

**Response:**
```json
{
  "cost":   {"rows": [{"dimension":"model","key":"jpcp","gpuHours":6.0,"costUsd":16.0,"runs":2}],
             "total_gpu_hours": 7.0, "total_cost_usd": 20.0},
  "budget": {"budgets": [{"project":"eu-hpc","period":"monthly","costRatio":1.33,"overBudget":true}],
             "consumed_cost_usd": 20.0},
  "carbon": {"totals": {"kwh": 15.0, "co2e_g": 4500.0}, "co2e_kg": 4.5,
             "uncertainty": 0.3, "methodology": "Energy = GPU-hours × TDP × PUE; …"},
  "unitEconomics": {"costPerTrainingRun": 6.67}
}
```

Carbon figures always carry `uncertainty` + `methodology` (no false precision, F13 R3).

---

### `GET /api/v1/search` (Global search, F2)

Federated global search across pages / models / HPC jobs / audit events. Requires `viewer`.
BFF-composed (partial-failure safe). Query params: `q` (search string), `limit` (default 20).

**Response:**
```json
{
  "search": {
    "query": "jpcp", "count": 3,
    "results": [
      {"kind": "model", "id": "jpcp", "label": "JPCP", "url": "/models/jpcp", "score": 100, "source": "mlflow"},
      {"kind": "job",   "id": "slurm-42", "label": "slurm-42 · JPCP (RUNNING)", "url": "/facility", "score": 80, "source": "scheduler"},
      {"kind": "audit", "id": "retrain_triggered JPCP", "label": "retrain_triggered JPCP", "url": "/audit", "score": 60, "source": "audit"}
    ],
    "groups": {"mlflow": ["…"], "scheduler": ["…"], "audit": ["…"]}
  }
}
```

Results are ranked (exact > prefix > word-boundary > substring > fuzzy subsequence) and grouped by
`source`; each carries the F1 entity `url`. A blank `q` returns no results.

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

### Bearer identity and scopes

Sensitive reads require a bearer credential with `read`; mutations require `write`.
`CONTROL_PLANE_CREDENTIALS_JSON` maps each bearer secret to its trusted `principal`, `tenant`, and
scope list. The server never accepts actor or tenant identity from a request body or ad hoc header.
Approvals and durable commands are tenant-scoped, and structured credentials can poll only flow
runs dispatched for their tenant.

The legacy `CONTROL_PLANE_TOKEN` remains valid as principal `legacy`, tenant `default`, with both
scopes and operator-wide flow lookup. Malformed structured JSON—or reuse of the legacy secret in the
map—fails all bearer authentication closed. Health/readiness, metrics, registry listing, and model
metadata/assets remain public. ModelZoo webhooks use their separate webhook secret.

### `GET /livez`, `GET /readyz`, and `GET /health`

`/livez` is process liveness. `/readyz` returns 200 only when startup checks, the approval store,
and the enabled ModelZoo poller are healthy; otherwise it returns 503. `/ready` remains a
compatibility alias for liveness.

`/health` is diagnostic and returns HTTP 200 even for a `starting` or `degraded` verdict. Use
`/readyz`, not `/health`, as the traffic-readiness probe.

**Response 200:**
```json
{
  "status": "ok",
  "prefect_api_url": "http://localhost:14200/api",
  "auth_configured": true,
  "models": {"JPCP": ["PM100Dataset"]},
  "pending_approvals": 1,
  "startup_checks": {"db": "ok", "registry": "ok", "token": "ok", "coordinator": "ok"},
  "poller": {"enabled": true, "last_ok_seconds_ago": 12, "stale": false, "leader": true},
  "runtime": {
    "state_backend": "postgres",
    "configured_coordinator": "db",
    "active_coordination": "db",
    "configured_event_publisher": "redis",
    "event_relay_enabled": true,
    "outbox": {"pending": 2, "published": 41, "poison": 0},
    "horizontal_scaling_safe": false,
    "horizontal_scaling_blockers": [
      "retrain_dedup_requires_client_key",
      "circuit_breaker_process_local"
    ]
  }
}
```

The runtime block reports what this process actually uses. Durable command records and Prefect
idempotency keys protect retrain dispatch across processes sharing the state backend. The selected
coordinator owns rate limits, retrain locks, and the singleton poller lease. The event relay drains
the same state database and reports pending, published, and poison rows. Circuit-breaker state and
runtime configuration remain local, so do not infer multi-replica safety from Postgres alone.

---

### `POST /retrain`

**Auth:** bearer credential with `write` scope.

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

### `GET /retrain/{flow_run_id}`

**Auth:** bearer credential with `read` scope. Structured credentials receive 404 for a flow run
outside their verified tenant. The legacy credential retains operator-wide lookup compatibility.

---

### `POST /api/changes` (Phase 11)

CI webhook — records changed model IDs as pending approvals. Does not fire training.

**Auth:** bearer credential with `write` scope. Created approvals inherit its verified tenant and
principal.

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
{"created": ["<approval-uuid-1>", "<approval-uuid-2>"]}
```

---

### `GET /approvals` (Phase 11)

List approval records. Filter with `?status=pending|approved|rejected`.

**Auth:** bearer credential with `read` scope. Only records owned by its verified tenant are listed.

**Response 200:**
```json
[
  {
    "id": "<approval-uuid>",
    "model_id": "JPCP",
    "status": "pending",
    "commit_sha": "abc123",
    "commit_msg": "feat: improve JPCP features",
    "changed_files": ["pipelines/model_configs/jpcp_config.py"],
    "tenant": "team-a",
    "requested_by": "release-bot",
    "resolved_by": null,
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

**Auth:** bearer credential with `write` scope. Resolution is limited to its verified tenant.

**Response 200:**
```json
{"model_id": "JPCP", "flow_run_id": "xyz789", "status_url": "/retrain/xyz789"}
```

**Errors:**

| Code | Reason |
|---|---|
| 404 | No pending approval found for `model_id` |
| 502 | Prefect unreachable |

---

### `POST /reject/{model_id}` (Phase 11)

Reject a pending change. No training runs.

**Auth:** bearer credential with `write` scope. Resolution is limited to its verified tenant.

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

**Auth:** `X-Gitlab-Token: <MODELZOO_WEBHOOK_SECRET>` header. The endpoint returns 503 when the
secret is not configured and compares configured values in constant time.

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

**Auth:** `X-Hub-Signature-256: sha256=<HMAC-SHA256>` header. The endpoint returns 503 when the
secret is not configured.

**Request body:** GitHub push webhook payload (JSON)

**Response 200:** same shape as GitLab webhook.

---

### `GET /modelzoo/status`

Per-model freshness snapshot.

**Auth:** bearer credential with `read` scope.

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

**Auth:** bearer credential with `read` scope.

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

**Auth:** bearer credential with `write` scope.

**Response 200:**
```json
{"new_commit": true, "commit_sha": "abc12345", "models_marked_stale": 3}
```

or `{"new_commit": false}` when already up-to-date.

---

### `GET /modelzoo/config`

Show current runtime ModelZoo integration config.

**Auth:** bearer credential with `read` scope.

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

**Auth:** bearer credential with `write` scope.

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
