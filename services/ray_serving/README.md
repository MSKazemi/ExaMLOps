# Ray Multi-Model Serving

Ray Serve deployment that loads **all Production-stage models** from MLflow at startup and exposes them through a single API. One Prefect, one MLflow, one Ray Serve — all training pipelines register to the same MLflow registry and all models are automatically served here.

## Architecture

```
MLflow Registry (Postgres backend)
        │
        │  startup: scan all Production models
        ▼
Ray Serve MultiModelServer
  ├── replicas: 1–N (RAY_NUM_REPLICAS)
  ├── GET  /models               list loaded models
  ├── GET  /health               liveness + per-model status
  ├── POST /predict/{model_name} generic prediction
  └── POST /reload               hot-reload from MLflow (no restart)

Ray Dashboard: http://localhost:8265
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Liveness + status of each loaded model |
| `GET`  | `/models` | List all loaded models with version / run_id |
| `POST` | `/predict/{model_name}` | Run inference (generic feature dict) |
| `POST` | `/reload` | Hot-reload all Production models from MLflow |
| `GET`  | `/docs` | Swagger UI |

## Predict request format

Features are passed as a flat JSON dict. Keys must match the column names the model was trained on.

**uc_power_model** example (one-hot encoded queue):
```json
POST /predict/uc_power_model
{
  "features": {
    "num_nodes": 2,
    "num_cores": 128,
    "walltime": 7200,
    "submitted_hour": 9,
    "queue_gpu": 0,
    "queue_large": 0,
    "queue_normal": 1
  }
}
```

Response:
```json
{
  "model_name": "uc_power_model",
  "model_version": "38",
  "run_id": "abc123...",
  "prediction": 142500.5
}
```

## Hot-reload after model promotion

After promoting a new model version to Production in MLflow, reload without restarting:
```bash
curl -X POST http://localhost:8001/reload
```

## Start / Stop

**Local (dev):**
```bash
make ray-serving-start      # single process, foreground
make start-all              # background, Docker stack + Ray Serve
make stop-all               # stop all background services
```

**Docker (full stack):**
```bash
make dev-up                 # starts postgres, mlflow, orchestrator, ray-serving
make dev-down               # stop all
```

**With optional monitoring:**
```bash
make monitoring-up          # add Prometheus (9090) + Grafana (3000)
make monitoring-down        # stop monitoring to free memory
```

## Env vars

| Variable | Default | Description |
|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | MLflow server |
| `MODEL_STAGE` | `Production` | Model stage to load |
| `RAY_NUM_REPLICAS` | `2` | Replicas per deployment |
| `RAY_SERVE_PORT` | `8001` | HTTP serving port |
| `RAY_METRICS_EXPORT_PORT` | `8080` | Prometheus metrics port |
