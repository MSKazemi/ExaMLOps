# Quick Start

Get from zero to serving predictions in five minutes.

## Prerequisites

- Docker with Compose v2
- `uv` ≥ 0.4 and Python 3.12+

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 1. Start the full stack

```bash
make bootstrap
```

This starts Postgres, MLflow, Prefect, and Ray Serve in Docker, then installs all Python dependencies. Once complete you should see:

| Service | URL |
|---|---|
| MLflow UI | http://localhost:15000 |
| Prefect UI | http://localhost:14200 |
| Ray Serve API | http://localhost:18001 |
| Ray Dashboard | http://localhost:18265 |
| ExaMLOps Dashboard | http://localhost:18099 |
| JupyterHub | http://localhost:18888 |

## 2. Train a model

Run all discovered models with dummy data (no downloads, completes in seconds):

```bash
exa pipeline run --dummy
```

This runs the full pipeline for every registered model × dataset pair:
`data extraction → HPC submit → evaluate → MLflow log → promote`

To train a specific model:

```bash
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
```

## 3. Promote to Production

The pipeline promotes automatically if RMSE is below the threshold defined in the model config. Check the MLflow UI to confirm the `@Production` alias was set:

```bash
open http://localhost:15000/#/models
```

## 4. Serve a prediction

Phase 3 keeps Ray Serve auto-synced with MLflow via polling + Prefect webhook, so you usually don't need a manual reload. If you want to force one:

```bash
exa serve reload
```

Send a prediction. Phase 3: optionally pin a specific lifecycle alias or raw version per request.

```bash
# Default (uses the @Production alias):
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8, "feature_2": 3.4}}'

# Hit the Canary alias instead:
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2}, "alias": "Canary"}'

# Hit a specific historical version (lazy-loaded via the LRU cache):
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2}, "version": "3"}'
```

Response (the `alias` and `model_version` echo what was actually served):

```json
{
  "model_name": "JPCP",
  "alias": "Production",
  "model_version": "1",
  "run_id": "abc123...",
  "prediction": 142.7
}
```

## 5. Trigger a retrain via the control plane (Phase 4)

Before retrain works, a Prefect deployment must exist. Run this once in a **separate terminal** (it registers the deployment and runs a worker — keep it running):

```bash
exa pipeline deploy
# or without a cron schedule:
exa pipeline deploy --no-schedule
```

Then trigger a retrain from your original terminal:

```bash
make control-plane-up
exa retrain JPCP --dataset PM100Dataset --dummy
```

Or via the `exa` CLI:

```bash
exa retrain JPCP --dummy
```

Or directly with curl:

```bash
curl -X POST http://localhost:18002/retrain \
  -H "Authorization: Bearer ${CONTROL_PLANE_TOKEN:-changeme}" \
  -H "Content-Type: application/json" \
  -d '{"model_name":"JPCP","dataset_name":"PM100Dataset","is_dummy":true}'
```

See [Control Plane](control-plane.md) for the full API.

## 6. Try a different data source (Phase 1)

```bash
# Pull from MinIO (after seeding s3://examlops-data/PM100/job_table.parquet):
exa pipeline run --model JPCP --dataset PM100Dataset --backend minio

# Snapshot the dataplane simulator and train on it:
exa pipeline run --model JPCP --dataset PM100Dataset --backend dataplane --dummy
```

## 7. Scaffold a new model (Phase 2)

```bash
exa scaffold DemoAD --task anomaly_detection --type classification
exa pipeline list                # confirm DemoAD appears
.venv/bin/pytest tests/unit/test_demoad.py
```

See [Add a New Model](add-a-new-model.md) for the 5-minute walkthrough.

## 8. Check service health

```bash
exa status
curl http://localhost:18099/api/health
curl http://localhost:18001/health       # Ray Serve (lists every loaded alias)
curl http://localhost:18002/health       # Control plane (Phase 4)
exa modelzoo status                          # Phase 12 — freshness badges
exa modelzoo events                          # recent ModelZoo push events
```

ModelZoo freshness badges on the dashboard Models page will show `CURRENT` or `UPDATED` depending on whether any ModelZoo repository pushes have been recorded since the last retrain.

## 9. Start the notebook environment (Phase 9)

```bash
make jupyter-up
```

Opens JupyterHub at http://localhost:18888. Log in with your credentials. Each user gets an isolated JupyterLab container pre-configured to reach all stack services (MLflow, MinIO, Ray Serve, Prefect) by internal hostname.

**VS Code:** Jupyter extension → *Specify Jupyter Server* → `http://localhost:18888/user/<username>/?token=<token>`

See [JupyterHub Guide](jupyter.md) for user management and full setup details.

## Next steps

- Browse all UIs and APIs → [Interfaces guide](interfaces.md)
- Add your own model → [Add a new model](add-a-new-model.md)
- Architecture overview → [Architecture](architecture.md)
- Trigger retrains from clients → [Control Plane](control-plane.md)
- Notebook environment → [JupyterHub Guide](jupyter.md)
- Add a non-sklearn framework → see CLAUDE.md "Framework Extensibility (Phase 5)"
- Connect to a real HPC cluster → CLAUDE.md "HPC modes" section
