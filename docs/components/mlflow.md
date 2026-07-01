# MLflow — Experiment Tracking & Model Registry

MLflow is the central store for all training runs, metrics, and model versions. It is the link between the training pipeline and Ray Serve — only models with a `@Production` alias in the MLflow registry are served.

## Access

- **UI:** http://localhost:5000
- **API:** http://localhost:5000/api/2.0/mlflow/...

## What ExaMLOps stores in MLflow

For each training run, `log_mlflow_task` records:

| What | MLflow API |
|---|---|
| Model name and dataset | `mlflow.log_param()` |
| Estimator class name | `mlflow.log_param()` |
| Hyperparameters | `mlflow.log_params()` |
| RMSE, MAPE, MSE | `mlflow.log_metrics()` |
| Trained sklearn model | `mlflow.sklearn.log_model()` |

Each run is logged to an **experiment** named `{model_name}_{dataset_name}` (e.g. `jpcp_pm100dataset`).

## Model registry

After logging, the model is registered as `{config.get_inference_params()["model_id"]}` (e.g. `"JPCP"`). Every successful training run creates a new version.

### Promotion gate

`promote_task` reads the model config's `get_inference_params()` and compares the evaluation metric to the threshold:

```python
# Example from a model config:
def get_inference_params():
    return {
        "model_id":             "JPCP",
        "promotion_metric":     "rmse",
        "promotion_threshold":  50.0,
        "promotion_direction":  "lower_is_better",
        ...
    }
```

If `rmse ≤ 50.0`:

```python
client.set_registered_model_alias("JPCP", "Production", version)
```

If the threshold is not met, the model stays in `Staging` and is not served.

```
RMSE = 42.3 ≤ 50.0  →  @Production alias set  →  Ray Serve loads it
RMSE = 63.1 > 50.0  →  stays Staging           →  not served
```

## Viewing experiments and runs

1. Open http://localhost:5000
2. Click an experiment (e.g. `jpcp_pm100dataset`)
3. Compare runs by RMSE/MAPE
4. Click a run to see params, metrics, and the registered model artifact

## Managing the model registry

```bash
# Via MLflow UI
open http://localhost:5000/#/models

# Via Python SDK
python3 - <<'EOF'
import mlflow
mlflow.set_tracking_uri("http://localhost:5000")
client = mlflow.MlflowClient()

# List all registered models
for m in client.search_registered_models():
    print(m.name, [a.alias for a in m.aliases])

# Manually set Production alias
client.set_registered_model_alias("JPCP", "Production", "3")

# Remove an alias
client.delete_registered_model_alias("JPCP", "Production")
EOF
```

## Promote after the fact

If you want to manually promote a specific version without re-training:

```bash
python3 - <<'EOF'
import mlflow
mlflow.set_tracking_uri("http://localhost:5000")
client = mlflow.MlflowClient()
client.set_registered_model_alias("JPCP", "Production", "2")
EOF

# Then hot-reload Ray Serve
exa serve reload
```

## Backend store

MLflow uses Postgres as the backend store (configured in Docker Compose). The tracking URI used inside the stack is `http://mlflow:5000` (Docker DNS). Outside the stack it is `http://localhost:5000`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | Used by the pipeline and Ray Serve |
| `MODEL_STAGE` | `Production` | Alias Ray Serve looks for when loading models |

## Useful MLflow CLI commands

```bash
# List experiments
mlflow experiments list --tracking-uri http://localhost:5000

# List all runs in an experiment
mlflow runs list --experiment-name jpcp_pm100dataset \
  --tracking-uri http://localhost:5000

# Download an artifact from a run
mlflow artifacts download \
  --run-id <run_id> \
  --artifact-path model \
  --tracking-uri http://localhost:5000
```
