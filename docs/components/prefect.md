# Prefect — Pipeline Orchestration

Prefect is the orchestration engine that runs the training pipeline for every model × dataset pair discovered by ExaMLOps.

## What it does here

- Wraps each training stage into a **Prefect task** with retries and caching
- Runs a **generic `training_flow`** for every auto-discovered (model, dataset) combination
- Provides a UI to monitor, retry, and inspect runs at **http://localhost:14200**
- Reports task status, logs, and timing per run

## Pipeline structure

Every model runs through the same seven-task flow:

```
training_flow(model_name, dataset_cls_name)
  │
  ├─ 1. data_extraction_task
  │     Instantiates the model with hyperparams and builds the train dataloader.
  │     No training happens here.
  │
  ├─ 2. slurm_submit_task
  │     mock mode : trains the model inline, saves estimator to a temp .pkl
  │     slurm mode: submits an sbatch job, returns job_id immediately
  │
  ├─ 3. slurm_wait_task
  │     mock mode : returns immediately (artifact already exists)
  │     slurm mode: polls squeue/sacct until COMPLETED / FAILED / CANCELLED
  │
  ├─ 4. result_fetch_task
  │     Loads the trained estimator from the .pkl artifact path.
  │     Injects it into a fresh model instance (preserving hyperparams).
  │
  ├─ 5. evaluate_task
  │     Runs the model on the validation split.
  │     Computes RMSE, MAPE, MSE.
  │
  ├─ 6. log_mlflow_task
  │     Logs params + metrics + model artifact to MLflow.
  │     Registers the model in the MLflow model registry.
  │
  └─ 7. promote_task
        Checks if the promotion metric (e.g. RMSE) passes the threshold
        defined in the model config. If yes, sets the @Production alias.
```

## Running pipelines

```bash
# List all auto-discovered models and datasets
exa pipeline list

# Train all models with dummy data (fast, no downloads)
exa pipeline run --dummy

# Train all models with real Zenodo data (production)
exa pipeline run --env prod

# Train a single model
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
```

**Via the `exa` CLI:**

```bash
exa pipeline deploy                      # register nightly deployments for all models
exa pipeline deploy --no-schedule        # register without cron schedule
exa pipeline deploy --model JPCP         # register for one model only
exa pipeline deploy --env prod           # with YAML env overlay
```

Or call the generator directly:

```bash
# List discovered models
.venv/bin/python pipelines/pipeline_generator.py --list

# Run a specific model
.venv/bin/python pipelines/pipeline_generator.py \
  --model JPCP --dataset PM100Dataset --dummy
```

## Auto-discovery

The pipeline generator scans `pipelines/models/*.yaml` at import time and auto-wires everything (Phase 14):

```
pipelines/models/*.yaml
  → each YAML file declares the full model config
  → config_class: field points to the Python shim for transforms
  → registered as a YAMLBackedConfig in MODEL_REGISTRY
```

To add a new model, run `exa scaffold` — it generates both the Python shim and the per-model YAML. No changes to `pipeline_generator.py` needed.

See [ModelZoo](modelzoo.md) for how to write a model.

## Task retries and caching

| Task | Retries | Cache |
|---|---|---|
| `data_extraction` | 0 | None |
| `slurm_submit` | 1 | None |
| `slurm_wait` | 0 | None |
| `result_fetch` | 0 | None |
| `evaluate` | 0 | None |
| `log_mlflow` | 0 | None |
| `promote` | 0 | None |

Tasks use `NO_CACHE` to ensure each run re-executes all steps from scratch. The `slurm_submit` retry handles transient HPC queue rejections.

## Prefect UI

Open http://localhost:14200 to:

- See all flow runs and their status (Completed / Failed / Running)
- Inspect per-task logs and timing
- Retry failed runs
- View the full DAG for each flow

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `PREFECT_API_URL` | `http://localhost:4200/api` | Prefect server endpoint |
| `EXAMLOPS_SLURM_MODE` | `mock` | `mock` = train inline, `slurm` = submit to HPC |
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | Where to log experiments |

## Connecting to a remote Prefect server

```bash
export PREFECT_API_URL=http://your-prefect-server:4200/api
exa pipeline run --dummy
```

## Common issues

**Flow not finding models** — run `exa pipeline list` to confirm discovery works. Check that `MODEL_CLASS` is set on the config class.

**Task failed: MLflow connection refused** — ensure `make stack-up` has been run and MLflow is healthy at port 5000.

**`slurm_submit` fails in slurm/flux mode** — the compute node needs the repo, a Python env, and the scheduler CLI on `PATH` (set `EXAMLOPS_HPC_REMOTE_{REPO,PYTHON}`). Add `--dummy` for a fast cluster smoke test (it is forwarded to the node), or switch to `EXAMLOPS_SLURM_MODE=mock` for local development. Full walkthrough: [../guides/hpc-training-workflow.md](../guides/hpc-training-workflow.md).

---

## Production: Prefect Blocks for credential management

> **TODO (production)** — The current dev setup passes MinIO and MLflow credentials as environment variables via the Makefile. For a shared Prefect server, use **Prefect Blocks** instead so credentials can be rotated from the UI without touching code or redeploying flows.

### What Prefect Blocks are

Blocks are named, typed secrets stored in the Prefect backend (Postgres). A flow loads a block by name at runtime. Updating the block value in the UI immediately affects all future runs — no code change, no redeploy.

### MinIO Credentials block

The `MinIOCredentials` block (from `prefect-aws`) wraps a boto3 session pointed at a MinIO-compatible endpoint. It is the production-ready replacement for passing `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `MLFLOW_S3_ENDPOINT_URL` as env vars.

#### 1 — Install the collection

```bash
uv pip install prefect-aws
```

#### 2 — Create the block (Prefect UI)

Go to **http://your-prefect-server:4200 → Blocks → + → MinIO Credentials** and fill in:

| Field | Value |
|---|---|
| Block name | `examlops-minio` (or per-environment: `examlops-minio-prod`) |
| Minio Root User | MinIO access key |
| Minio Root Password | MinIO secret key |
| Endpoint URL | `https://minio.your-cluster:9000` |
| Use SSL | checked (production) |
| Verify | checked (production) |
| Region Name | leave blank for self-hosted MinIO |

#### 3 — Or create the block via Python

```python
from prefect_aws import MinIOCredentials, AwsClientParameters

block = MinIOCredentials(
    minio_root_user="your-access-key",
    minio_root_password="your-secret-key",
    aws_client_parameters=AwsClientParameters(
        endpoint_url="https://minio.your-cluster:9000",
    ),
)
await block.save("examlops-minio", overwrite=True)
```

Run once, e.g. from a bootstrap script or CI, to register the block in the Prefect server.

#### 4 — Use the block in a flow task

```python
from prefect_aws import MinIOCredentials

async def get_minio_client():
    creds = await MinIOCredentials.load("examlops-minio")
    session = creds.get_boto3_session()
    return session.client("s3")
```

Tasks that upload/download MLflow artifacts would call `get_minio_client()` instead of reading env vars.

### Why this matters in production

| Concern | Env vars (current dev approach) | Prefect Blocks |
|---|---|---|
| Credential rotation | Requires restarting workers/containers | Update in UI, takes effect on next run |
| Multi-environment | Separate `.env` files per env | One block per env (`examlops-minio-dev`, `examlops-minio-prod`) |
| Audit trail | None | Prefect logs block reads per run |
| Secret encryption at rest | Depends on shell/CI config | Encrypted in Prefect's Postgres backend |

### Other blocks worth adding for production

| Block type | Purpose |
|---|---|
| `Secret` | Store the MLflow tracking URI or other sensitive config |
| `SlackWebhook` | Send alerts when a flow fails |
| `GitHubCredentials` | Authenticate model artifact pushes to GitHub |

These are all available in the Prefect Block catalogue under **Blocks → +** in the UI.

---

## Per-model deployments via YAML registry (Phase 14)

By default `exa pipeline deploy` creates a single `scheduled_training_flow` deployment that trains every model. With per-model YAML files, `exa pipeline deploy-env` creates **one Prefect deployment per enabled model**, each with its own schedule, work pool, and concurrency limit.

### Where are the model config files?

Per-model config files live at **`pipelines/models/<name>.yaml`** — one per model. These are the single source of truth for per-model pipeline configuration: datasets, features, lifecycle thresholds, Prefect schedule, serving aliases, and inference schema.

Validate all YAML files against their Python shims:

```bash
exa pipeline validate
# → runs pytest -v -k yaml on test_registry_integrity.py
```

To add a new model with a pre-populated YAML:

```bash
exa scaffold MyModel --task performance_prediction --type classification
# → generates pipelines/models/mymodel.yaml + Python shim + model + tests
```

Edit `pipelines/models/<name>.yaml` to tune lifecycle thresholds, then commit.

Each model's Prefect configuration is in its `prefect:` YAML section:

```yaml
# pipelines/models/jpcp.yaml
prefect:
  schedule: "0 2 * * *"
  deployment_name: examlops-jpcp-nightly
  work_pool: default-agent
  concurrency_limit: 1
```

The legacy `exa pipeline deploy` (single global deployment) still works unchanged when no `--registry` flag is provided.
