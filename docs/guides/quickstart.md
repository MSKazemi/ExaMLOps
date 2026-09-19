---
description: "Install ExaMLOps and go from zero to serving predictions in five minutes: start the local stack, train a model on dummy data, promote it and query it through Ray Serve — no HPC cluster needed."
---

# Quick Start

Get from zero to serving predictions in five minutes.

## Prerequisites

- Docker with Compose v2
- `uv` ≥ 0.4 and Python 3.12+

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 0. Just the CLI

The full stack below is what you want on a workstation. If all you need is to *talk* to an
ExaMLOps platform that already runs somewhere — a login node, a laptop, a CI job — install
the CLI on its own. It has no Docker, no MLflow and no GPU dependency:

```bash
uv pip install examlops        # or: pip install examlops
exa --version
exa status                     # points at the URLs in your active context
```

Until the package is on PyPI, install the signed wheel straight from a GitHub Release
(verify it first as described in [Releases](release-process.md)):

```bash
uv pip install https://github.com/MSKazemi/ExaMLOps/releases/download/v0.54.0/examlops-0.54.0-py3-none-any.whl
```

Heavier capabilities are **extras**, so the base install stays small enough for a login
node. Each one is lazily imported, and a command that needs a missing extra says which to
install rather than failing with a traceback:

| Extra | What it adds |
|---|---|
| `examlops[analysis]` | Statistical A/B analysis — `exa serve ab analyze` |
| `examlops[backup]` | Object-store and off-site backup tiers — `exa backup` |
| `examlops[coordination]` | Redis-backed cross-host locks, rate limits, deduplication, and event streams |
| `examlops[dataplane]` | Everything the [dataplane](dataplane.md) service needs — the union of the four extras below |
| `examlops[dataplane-sql]` | The `sql` dataplane connector — any SQLAlchemy URL (Postgres, MySQL, SQLite) |
| `examlops[dataplane-files]` | The `files`, `zenodo` and `rest` dataplane connectors — object storage, HTTP(S), SFTP, Zenodo records |
| `examlops[dataplane-kafka]` | The `kafka` dataplane connector — bounded batch reads from a topic |
| `examlops[dataplane-service]` | Run the dataplane HTTP service (`platform/services/dataplane`) — not needed just to pull from the CLI |
| `examlops[events]` | Publish to and consume from the NATS JetStream event backbone — `exa events tail`, `EventConsumer` |
| `examlops[fairness]` | Fairlearn's `MetricFrame` for fairness slice metrics — `exa fairness` (a pure-Python fallback gives the same numbers without it) |
| `examlops[finops]` | YAML/expression calculation providers — user-authored cost and carbon formulas |
| `examlops[guardrails-presidio]` | Presidio NER (person/place PII detection) as a supplement to the built-in regex PII detectors — needs a separate one-time spaCy model install |
| `examlops[mcp]` | Serve the platform to LLM agents — `exa mcp serve` |
| `examlops[oidc]` | Validate OIDC access tokens (RS256 against a JWKS) |
| `examlops[postgres]` | Talk to a Postgres datastore instead of SQLite |
| `examlops[serving-sglang]` | In-process SGLang engine (GPU host) |
| `examlops[serving-vllm]` | In-process vLLM engine for offline batch scoring (GPU host) |
| `examlops[synth]` | Synthetic data generation and its release gate — `exa data synth` |
| `examlops[vector]` | pgvector vector store (`EXAMLOPS_VECTOR_BACKEND=pgvector`), independent of where platform state lives |

Combine them as usual — `uv pip install 'examlops[mcp,analysis]'`. `[dev]` on the repository
root pulls the whole development environment including the test dependencies.

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

# Pull an external source into a versioned snapshot with the dataplane:
exa dataplane sources create pm100 --connector zenodo --spec-json '{"record": 10127767}'
exa dataplane pull pm100
```

To train on that snapshot, give the model's dataset entry `backend: dataplane` and a
`dataplane: {source: pm100}` binding; see the [Dataplane guide](dataplane.md).

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
- Add a non-sklearn framework → [Add a new model](add-a-new-model.md)
- Connect to an HPC cluster → [Distributed training](distributed-training.md)
