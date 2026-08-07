# ExaMLOps

End-to-end MLOps platform for HPC workload management in large European research projects. Covers auto-discovery-based training pipelines (Prefect), HPC job orchestration (Slurm adapter), model versioning (MLflow registry with multi-stage lifecycle), YAML-driven model registry with per-environment overlays, multi-model serving (Ray Serve) with a batching inference pipeline (`@serve.batch`), real-time metrics (Prometheus + Grafana), centralized logs (Loki), a control-plane API with sysadmin approval gate (push-to-serve pipeline), a React 19 + FastAPI dashboard, a SeanerBUS HPC message bridge, **Skipper** (a LangGraph management agent with a kube-q chat frontend), and the `exa` platform CLI for operator use.

## Quick Start

```bash
make help       # list all available targets
make bootstrap  # one-shot: start dev stack + install all deps
```

## Running the Auto-Pipeline

The pipeline auto-discovers all registered models and executes train → evaluate → MLflow log → promote for every model × dataset combination.

**Start infrastructure** (MLflow, Postgres, Prefect, Ray Serve, MinIO, Dashboard):
```bash
make stack-up
```

**List registered models:**
```bash
exa pipeline list
# JPCP: ['PM100Dataset', 'FDataDataset']
# MACK: ['FDataDataset']
# MCBound: ['FDataDataset']
```

**Dry run with dummy data** (fast, no Zenodo download):
```bash
exa pipeline run --dummy
```

**Full run with real Zenodo data** (production use):
```bash
exa pipeline run --registry pipelines/model_registry.yaml --env prod
```

**Single model/dataset:**
```bash
exa pipeline run --model JPCP --dataset PM100Dataset --dummy          # dummy data
exa pipeline run --model JPCP --dataset PM100Dataset --backend minio  # pull from MinIO
```

**Run with YAML registry (environment overlay):**
```bash
exa pipeline run --registry pipelines/model_registry.yaml --env dev --dummy
exa pipeline run --registry pipelines/model_registry.yaml --env prod
exa pipeline export-registry       # export current auto-discovered state to model_registry.yaml
```

**View results:**
```
http://localhost:15000   # MLflow UI
http://localhost:14200   # Prefect UI
```

## Key Make Targets

```bash
# Infrastructure
make bootstrap              # one-shot setup
make stack-up               # start full dev stack
make stack-down             # stop containers (volumes preserved)
make stack-wipe             # DESTRUCTIVE: remove containers, volumes, images
make stack-restart          # restart without rebuild
make stack-logs             # tail docker-compose logs
make monitoring-up          # start Prometheus + Grafana + Loki + Promtail
# cd ../seanerbus && docker compose up -d   # start real SeanerBUS + reqgen
make seanerbus-up           # start bridge (connects to real SeanerBUS)

# Exa CLI: pipelines, deployments, serving, and production state
exa pipeline list
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
exa pipeline run --registry pipelines/model_registry.yaml --env prod
exa pipeline deploy --model JPCP --registry pipelines/model_registry.yaml --env staging
exa pipeline export-registry
exa scaffold DemoAD --task anomaly_detection --type classification
exa serve check
exa serve infer-check
exa retrain JPCP --dataset PM100Dataset --dummy

# Control plane infrastructure
make control-plane-up       # start retrain API on :18002

# Dashboard
make dashboard-up           # build + start dashboard on :18099
make dashboard-logs         # tail dashboard logs
make dashboard-check        # run backend pytest + frontend npm test

# SeanerBUS bridge
make seanerbus-up           # start SeanerBUS bridge
make seanerbus-down         # stop SeanerBUS bridge

# Approval gate (Phase 11)
exa approvals list
exa approvals approve JPCP
exa approvals reject JPCP --reason "x"

# Management agent
make skipper                  # start LangGraph management agent (platform/services/agent/)

# exa CLI (primary operator interface)
# Install once: uv pip install -e ".[dev]"  then use exa from anywhere
exa status                          # platform snapshot
exa approvals list                  # pending approvals
exa models list                     # registered models
exa --help                          # full command reference

# JupyterHub
make jupyter-up             # build images + start JupyterHub on :18888
make jupyter-down           # stop JupyterHub (user volumes preserved)
make jupyter-logs           # tail JupyterHub logs

# Quality
make check                  # lint + typecheck + test + dashboard-check
make lint                   # ruff linter
make test                   # full test suite
exa status                  # show services, approvals, and production state
```

## Service URLs

### Local Development

| Service | URL |
|---|---|
| Dashboard | http://localhost:18099 |
| MLflow UI | http://localhost:15000 |
| Prefect UI | http://localhost:14200 |
| Ray Serve API | http://localhost:18001 |
| Ray Dashboard | http://localhost:18265 |
| Control Plane | http://localhost:18002 |
| SeanerBUS Bridge Status | http://localhost:18003 |
| JupyterHub | http://localhost:18888 |
| MinIO Console | http://localhost:19001 |
| Prometheus | http://localhost:19090 |
| Grafana | http://localhost:13000 |

### Remote Server lxp-cpu01 (<REMOTE_HOST>)

Same ports as local — e.g. http://<REMOTE_HOST>:18099 for the Dashboard. Accessible via `ssh lxp` with all ports forwarded to localhost.

## Adding a New Model

**Recommended — scaffold via cookiecutter:**
```bash
exa scaffold DemoAD --task anomaly_detection --type classification
```

This produces a model file, config file, and unit test wired for the Phase 1 backend kwarg and Phase 3 multi-stage lifecycle. See [docs/guides/add-a-new-model.md](docs/guides/add-a-new-model.md) for the walkthrough.

**Manual path:**
1. Implement `SeanergysSklearnModel` (or `SeanergysPyTorchModel` / `SeanergysHuggingFaceModel`) under `modelzoo/seanergys_modelzoo/models/tasks/`.
2. Create `pipelines/model_configs/<model>_config.py` with `MODEL_CLASS`, `SUPPORTED_DATASETS`, `get_train_components(..., backend_name=None)`, and `get_inference_params()`.

The pipeline discovers and runs it automatically; CI enforces registry integrity.

## Repository Layout

```
ExaMLOps/
├── docs/                       # Documentation
│   ├── components/             # Per-service component docs
│   ├── guides/                 # Quickstart, add-a-model, SeanerBUS, etc.
│   └── reference/              # Commands, env vars, CLI, API reference
├── modelzoo/                   # seanergys-modelzoo model library (poetry)
│   └── seanergys_modelzoo/
│       ├── models/             # Concrete model implementations + framework adapters
│       └── datasets/           # SeanergysDataset subclasses + pluggable backends
├── pipelines/
│   ├── pipeline_generator.py   # Auto-discovery orchestration (Prefect flows)
│   ├── model_loader.py         # Typed YAML loader + scan_model_yamls()
│   ├── models/                 # Per-model YAML configs (single source of truth, Phase 14)
│   ├── model_configs/          # Transforms-only Python shims (no base class, Phase 14)
│   └── deploy.py               # Prefect deployment registration
├── serving/
│   ├── ray_serving/            # Multi-model Ray Serve inference :18001
│   └── inference_pipeline/     # Ray Serve DeploymentGraph (Phase 10)
└── platform/                   # Platform area (workspace coordinator: examlops-workspace)
    ├── clients/                # SeanerBUS bridge + seanerbus_sim.py + dummy client
    ├── ci/                     # CI helper scripts (notify_model_changes.py)
    ├── infra/
    │   ├── docker-compose/     # Dev stack (profiles: default / monitoring / seanerbus / dev)
    │   └── slurm-adapter/      # HPC/Slurm integration (mock + real)
    ├── services/
    │   ├── agent/              # LangGraph management agent + skipper/ package
    │   ├── control_plane/      # FastAPI retrain API :18002
    │   └── dashboard/          # React 19 + FastAPI dashboard :18099
    └── cli/                    # Installable `examlops` dist (uv pip install -e ".[dev]")
        └── src/examlops/       # Shared schemas + `exa` platform CLI (Typer)
            └── cli/            # exa CLI: approvals/models/retrain/predict/serve/pipeline/seanerbus/stack/config
```

## Python Environments

Two separate environments coexist:

| Directory | Toolchain | Purpose |
|---|---|---|
| repo root (`.venv/`) | `uv` | Pipeline orchestration, Ray Serve, CI |
| `modelzoo/` | `poetry` | Model library (`seanergys-modelzoo` package) |

Activate root env: `source .venv/bin/activate`

## CI/CD

- `.github/workflows/ci.yml` — three parallel jobs (`modelzoo`, `infra`, `examlops`) on PRs and main
- `.github/workflows/deploy.yml` — retired (commented out); `.gitlab-ci.yml` deploys to `lxp-cpu01` via `deploy:lxp`
- `.gitlab-ci.yml` — GitLab mirror of the GitHub workflow

Run all CI checks locally: `make ci`

## Documentation

- [Quickstart](docs/guides/quickstart.md)
- [System Overview](docs/architecture/system-overview.md)
- [Command Reference](docs/reference/commands.md)
- [Environment Variables](docs/reference/env-vars.md)
- [SeanerBUS Integration](docs/guides/seanerbus.md)
- [Add a New Model](docs/guides/add-a-new-model.md)
- [exa CLI Reference](docs/reference/commands.md#exa-cli)
- [Approval Gate](docs/guides/control-plane.md#approval-gate-phase-11)
