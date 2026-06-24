# Ray Serve — Multi-Model Inference

Ray Serve hosts every aliased model from the MLflow registry under a single deployment. A `POST /predict/{model_name}` call routes to the right model version, with optional per-request alias or version selection (Phase 3).

## Start / stop

```bash
# Start Ray Serve as part of the full Docker stack
make stack-up

# Or start/rebuild only the Ray Serve compose service with exa
exa stack up --service ray-serving

# Check it is running
exa serve check
curl http://localhost:18001/health
```

## API endpoints

All endpoints are served on port **18001** by default.

### `GET /health`

Liveness + readiness check. Reports per-model status.

```bash
curl http://localhost:18001/health
```

```json
{
  "status": "ok",
  "models_loaded": 3,
  "models": {
    "JPCP": {"version": "3", "run_id": "abc123", "alias": "Production", "status": "ok"},
    "MACK": {"version": "7", "run_id": "def456", "alias": "Production", "status": "ok"}
  }
}
```

`status` is `"ok"` if at least one model is loaded, `"degraded"` if none are.

### `GET /models`

List all loaded models (hot set).

```bash
curl http://localhost:18001/models
```

```json
[
  {"model_name": "JPCP", "model_version": "3", "run_id": "abc123", "alias": "Production", "status": "ok"},
  {"model_name": "JPCP", "model_version": "2", "run_id": "bcd234", "alias": "Canary",     "status": "ok"}
]
```

### `POST /predict/{model_name}`

Run inference. The `features` dict must contain the column names the model was trained on.

**Basic request** (uses default alias, i.e. `MODEL_STAGE=Production`):

```bash
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8, "feature_2": 3.4}}'
```

**With alias selection** (Phase 3):

```bash
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8}, "alias": "Canary"}'
```

**With raw version selection** (Phase 3):

```bash
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8}, "version": "5"}'
```

Resolution order: `alias` → `version` → `MODEL_STAGE` default.

**Response:**

```json
{
  "model_name": "JPCP",
  "model_version": "3",
  "run_id": "abc123",
  "alias": "Production",
  "prediction": 142.7
}
```

**Errors:**

| Code | Reason |
|---|---|
| 404 | Model not loaded (alias not found, version not in cache, or name typo) |
| 500 | Feature mismatch or model exception |

### `POST /reload`

Hot-reload: re-scan MLflow and refresh the entire hot set (all aliases in `RAY_PRELOAD_ALIASES`).

```bash
curl -X POST http://localhost:18001/reload
```

```json
{"reloaded": ["JPCP", "MACK"], "count": 2}
```

No restart needed. Existing replicas continue serving during the reload.

### `POST /reload/{model_name}`

Hot-reload a single model only. Called automatically by Prefect's `promote_task` after a new Production alias is set.

```bash
curl -X POST http://localhost:18001/reload/JPCP
```

### `GET /docs` / `GET /redoc`

FastAPI Swagger UI and ReDoc — interactive API documentation generated automatically.

## How models are loaded

### Hot set (startup and on `/reload`)

On startup and on every `POST /reload` or `POST /reload/{name}`, the server loads every `(model, alias)` pair where the alias is in `RAY_PRELOAD_ALIASES` (default: `Production,Canary,Staging`):

1. Calls `mlflow.MlflowClient().search_registered_models()`
2. For each model and each alias in `RAY_PRELOAD_ALIASES`, attempts `client.get_model_version_by_alias(name, alias)`
3. Reads the `framework` tag on the model version and dispatches to the right MLflow loader (`mlflow.sklearn` / `mlflow.pytorch` / `mlflow.transformers`)
4. Stores the loaded model in the in-memory hot set

Models where an alias is not set are silently skipped for that alias.

### Framework dependencies in the serving image

Step 3 dispatches on the model version's `framework` tag, but the matching library must also be installed in the Ray Serve image — otherwise the load raises `ModuleNotFoundError` and the `(model, alias)` entry is dropped from the hot set (the failure is logged, not surfaced at startup). A model can therefore train and register successfully in MLflow yet never appear in `GET /models`.

Add the model's framework library to `serving/ray_serving/requirements.txt` and rebuild the image. Currently pinned there:

| Framework | Models | Required package |
|---|---|---|
| sklearn | JPCP, MCBound | `scikit-learn` (always present) |
| xgboost (sklearn-flavour) | MACK (`XGBClassifier`) | `xgboost==3.2.0` |

Pin the same major version used to train the model so the pickled estimator deserializes cleanly.

### On-demand version cache (Phase 3)

Per-request `version=` lookups that are not in the hot set are loaded on demand into a bounded LRU cache (size controlled by `RAY_VERSION_CACHE_SIZE`, default 8). The least-recently-used entry is evicted when the cache is full.

## Auto-reload mechanisms (Phase 3)

Two complementary mechanisms keep served models in sync with the MLflow registry:

### Polling

A background task re-scans MLflow alias state every `RAY_RELOAD_POLL_SECONDS` (default 60 seconds). Set to `0` to disable polling entirely (webhook-only mode).

### Webhook

Prefect's `promote_task` fires `POST /reload/{model_name}` immediately after setting a new Production alias. This gives sub-second propagation after a successful pipeline run. Polling acts as a safety net in case the webhook is missed.

## Replicas and scaling

Each deployment runs `RAY_NUM_REPLICAS` replicas (default: 2). Each replica holds the full hot set independently — no shared state between replicas.

Ray Serve autoscales replicas based on traffic if you configure `autoscaling_config` in the deployment decorator (not enabled by default).

## Metrics

Ray Serve exports Prometheus metrics on port **8080** (configured via `RAY_METRICS_EXPORT_PORT`).

| Metric | Type | Labels |
|---|---|---|
| `examlops_predict_requests_total` | Counter | `model_name`, `version`, `alias`, `status` (`success`/`error`/`not_found`) |
| `examlops_predict_latency_seconds` | Histogram | `model_name`, `version` |
| `examlops_prediction_value` | Histogram | `model_name` |
| `examlops_models_loaded` | Gauge | `replica` |
| `examlops_reload_total` | Counter | `scope`, `status`, `replica` |

These are visualised in the Grafana **ExaMLOps Online Metrics** dashboard. See [Grafana](grafana.md).

## Ray Dashboard

The Ray cluster dashboard is at **http://localhost:18265**. It shows:

- Serve deployments and replica status
- Per-replica memory and CPU usage
- Request throughput and error rates
- Actor logs

## Configuration

| Env variable | Default | Purpose |
|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://localhost:15000` | MLflow server to load models from |
| `MODEL_STAGE` | `Production` | Default alias when no `alias` or `version` is given in the request |
| `RAY_NUM_REPLICAS` | `2` | Number of replicas per deployment |
| `RAY_SERVE_PORT` | `8001` | Internal HTTP serving port (host-exposed as 18001) |
| `RAY_PRELOAD_ALIASES` | `Production,Canary,Staging` | Comma-separated aliases pre-loaded into the hot set on startup and reload |
| `RAY_VERSION_CACHE_SIZE` | `8` | LRU cache size for on-demand raw-version requests |
| `RAY_RELOAD_POLL_SECONDS` | `60` | Background alias-poll interval in seconds; set to `0` to disable |
| `RAY_SERVE_RELOAD_URL` | unset | Ray Serve URL used by Prefect's promote_task for the per-model reload webhook |
| `RAY_METRICS_EXPORT_PORT` | `8080` | Prometheus metrics port |
| `RAY_MODELS_DIR` | unset | Phase 14: directory of per-model YAML files (e.g., `pipelines/models`). Takes precedence over `RAY_REGISTRY_PATH` |
| `RAY_REGISTRY_PATH` | unset | Path to `model_registry.yaml`; enables per-model `serve_aliases`. Unset = use global `RAY_PRELOAD_ALIASES` for all models |
| `RAY_REGISTRY_ENV` | unset | Env overlay name (e.g., `prod`) loaded alongside `RAY_REGISTRY_PATH` |

### Per-model serve aliases (Phase 14 — per-model YAML)

By default all models share the same `RAY_PRELOAD_ALIASES` list. Set `RAY_MODELS_DIR` to enable per-model alias control from the per-model YAML files:

```bash
# Ray Serve reads per-model aliases from pipelines/models/*.yaml
RAY_MODELS_DIR=pipelines/models exa stack up --service ray-serving
```

Each model's `serving.aliases:` list in its YAML file controls which MLflow aliases are pre-loaded:

```yaml
# pipelines/models/jpcp.yaml
serving:
  model_id: jpcp
  aliases: [Production, Canary, Staging]
```

When `RAY_MODELS_DIR` is unset, `RAY_REGISTRY_PATH` (legacy monolithic registry) is tried next. When both are unset, Ray Serve falls back to the global `RAY_PRELOAD_ALIASES` env var.

## Benchmarking

```bash
# Run 200 random requests and report latency stats
exa serve benchmark

# Or run the client directly
.venv/bin/python platform/platform/clients/dummy_client.py --benchmark 200
```
