# Control Plane

The ExaMLOps control plane (`platform/services/control_plane/app.py`) is a **thin coordination layer** that sits between external event sources — CI webhooks, operator commands, drift triggers — and the Prefect training orchestrator.

It does not perform training, inference, or model management. It authorizes, routes, and records retrain requests.

## What it owns

### 1. Approval workflow state machine

When a GitLab CI pipeline calls `POST /api/changes` after a ModelZoo merge, the control plane creates a `pending_approvals` row in its own SQLite DB. Human operators approve or reject via the dashboard or CLI:

```
POST /approve/{model_id}   →  resolves Prefect deployment → fires flow run → records outcome
POST /reject/{model_id}    →  marks row rejected
```

### 2. ModelZoo freshness tracking

A background daemon thread polls the GitLab API every 5 minutes (configurable) for new commits to the ModelZoo repo. On new commit detection it marks all models stale in the `model_freshness` table and optionally fires automatic retrains when `MODELZOO_AUTO_RETRAIN=true`.

GitLab and GitHub webhooks (`POST /api/changes`) provide the push-triggered path for the same logic.

### 3. Retrain request authorization and forwarding

`POST /retrain` is the **single authorized entry point** for initiating training runs. The CLI (`exa retrain`), the dashboard Trigger button, and the drift auto-retrain subsystem all POST to this endpoint.

It validates the model and dataset against the YAML registry, then creates a Prefect flow run via `PrefectGateway` (plain `urllib`, no SDK).

### 4. Platform health aggregation

`GET /status` probes MLflow, Prefect, Ray Serve, and the Dashboard with 5-second timeouts and returns a unified service health dict. This is the primary data source for `exa status`.

### 5. Model metadata surface

Read-only endpoints serve YAML-derived model schemas, README content, and bundled images from the modelzoo filesystem — decoupling consumers from the raw filesystem layout.

| Endpoint | What it returns |
|---|---|
| `GET /modelzoo/status` | Freshness state per model (stale / current) |
| `GET /modelzoo/config` | Poller config: `auto_retrain`, `poll_interval_seconds` |
| `PUT /modelzoo/config` | Update poller config (in-memory only, resets on restart) |
| `GET /models/{name}/readme` | Raw model README from the YAML-adjacent file |
| `GET /models/{name}/schema` | Input/output schema from the model YAML |

### 6. Approval Prometheus metrics

Three metrics are computed from SQLite on each `/metrics` scrape:

| Metric | Type | Description |
|---|---|---|
| `examlops_control_plane_pending_approvals` | Gauge | Count of pending approvals |
| `examlops_control_plane_approval_events_total` | Counter | Approvals + rejections, labelled by action |
| `examlops_control_plane_oldest_pending_age_seconds` | Gauge | Age of the oldest pending approval |

## What it does NOT do

| Responsibility | Where it actually lives |
|---|---|
| Write to `platform.db` (audit, drift, traffic, costs) | CLI commands and SeanerBUS bridge |
| MLflow registry operations (alias promotion, version listing) | CLI-side only |
| Ray Serve beyond a liveness probe | CLI (`exa serve *`) and Ray Serve itself |
| SeanerBUS bridge state or inference stats | SeanerBUS bridge (`platform/clients/seanerbus_bridge.py`) |
| Drift detection or auto-retrain scheduling | `platform_db.py` drift tables + `exa drift *` CLI |
| Traffic split management | `platform_db.py` `traffic_rules` + `exa serve traffic` |
| Persisting its own config across restarts | Not implemented; `PUT /modelzoo/config` is in-memory |

## Integration topology

```
External triggers
  ├─ GitLab CI webhook       POST /api/changes
  ├─ GitHub webhook          POST /api/github/changes
  ├─ exa retrain             POST /retrain
  ├─ exa approvals approve   POST /approve/{model}
  └─ drift auto-retrain      POST /retrain  (via exa drift trigger)
          │
          ▼
    Control Plane (:18002)
    ├─ approvals SQLite (control_plane.db)
    ├─ modelzoo freshness SQLite (same DB)
    └─ PrefectGateway
          │
          ▼
    Prefect Orchestrator (:14200)
          │  runs training_flow
          ▼
    MLflow (:15000) ← artifact + metric storage
          │  aliases read by
          ▼
    Ray Serve (:18001) ← production inference
```

The control plane is purely a write-path forwarder and approval gate. It never reads trained model artifacts.

## API reference

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | none | Liveness + `db_ok` + `poller.alive` + pending approvals count |
| `GET` | `/status` | token | Full service health (MLflow, Prefect, Ray, Dashboard) |
| `GET` | `/metrics` | none | Prometheus metrics |
| `POST` | `/retrain` | token | Trigger a training run via Prefect |
| `POST` | `/api/changes` | none | GitLab CI webhook — mark models stale, queue approval |
| `POST` | `/api/github/changes` | none | GitHub webhook — same as above |
| `GET` | `/approvals` | token | List pending approvals |
| `POST` | `/approve/{model_id}` | token | Approve → fire Prefect run |
| `POST` | `/reject/{model_id}` | token | Reject with reason |
| `DELETE` | `/approvals/{approval_id}` | token | Delete a pending approval by UUID |
| `GET` | `/modelzoo/status` | token | Model freshness state |
| `GET` | `/modelzoo/config` | token | Poller configuration |
| `PUT` | `/modelzoo/config` | token | Update poller config (in-memory) |

Authentication is a `Bearer` token from `CONTROL_PLANE_TOKEN`. If unset the service returns 503 on token-required endpoints.

## Local development

```bash
make control-plane-up
make control-plane-logs
make control-plane-down
```

URL: http://localhost:18002
