# ExaMLOps

End-to-end MLOps platform for HPC workload management in large European research projects. Covers auto-discovery-based training pipelines (Prefect), HPC job orchestration (Slurm adapter), model versioning (MLflow registry with multi-stage lifecycle), YAML-driven model registry with per-environment overlays, multi-model serving (Ray Serve) with a batching inference pipeline (`@serve.batch`), real-time metrics (Prometheus + Grafana), centralized logs (Loki), a control-plane API with sysadmin approval gate (push-to-serve pipeline), a React 19 + FastAPI dashboard, a SeanerBUS HPC message bridge, a LangGraph management agent, and the `exa` platform CLI for operator use.

## Quick Start

```bash
make help       # list all available targets
make bootstrap  # one-shot: start dev stack + install all deps
```

## Phase Rollout

| Phase | Description | Status |
|---|---|---|
| 0 | CI/CD (GitHub Actions + GitLab mirror) + Loki/Promtail centralized logging | shipped |
| 1 | Pluggable dataset backends — Zenodo / MinIO / Dataplane | shipped |
| 2 | Cookiecutter model template (`exa scaffold`) + registry-integrity CI guard | shipped |
| 3 | Multi-stage MLflow lifecycle (Staging/Canary/Production/Archived) + Ray Serve hybrid version routing + auto-reload | shipped |
| 4 | Control Plane FastAPI (`POST /retrain`) + dataplane/client simulators with drift detection | shipped |
| 5 | Framework adapters — sklearn / pytorch / huggingface + LLM/agent skeleton | shipped |
| 6 | Dashboard: model detail page, MDEditor, MinIO image gallery, JWT auth, Alembic migrations | shipped |
| 7 | SeanerBUS HPC message bridge + monitoring page + GitLab CI retrain-on-merge | shipped |
| 8 | Dashboard service controls — start/stop/restart + status badges + log tail via Docker socket | shipped |
| 9 | YAML-driven model registry — base + env overlays, export, per-model lifecycle/backend/serve-alias overrides | shipped |
| 10 | Inference pipeline — Ray Serve DeploymentGraph with `@serve.batch`; SeanerBUS bridge updated to call `/infer-pipeline/infer` | shipped |
| 11 | Sysadmin approval gate — CI detects model changes → pending approval in SQLite → `exa approvals approve/reject` or dashboard → Prefect training; `exa` full-platform CLI | shipped |
| 12 | ModelZoo × Control Plane integration — GitLab/GitHub webhooks + background poller mark models stale on push; freshness badges in dashboard Models page; `exa modelzoo status/events/sync/config` CLI; Config page webhook section | shipped |
| 13 | Approval gate Prometheus metrics — `GET /metrics` on Control Plane; `examlops_approvals_pending`, `examlops_approval_events_total`, `examlops_approval_age_oldest_seconds`; Prometheus scrape job; provisioned Grafana dashboard | shipped |
| 14 | Per-model YAML config (`pipelines/models/<name>.yaml`) as single source of truth; `pipelines/model_configs/*.py` shrunk to transforms-only; `model_registry.yaml` deleted; `RAY_MODELS_DIR` env var | shipped |
| 15 | 4-area monorepo: `platform/` `pipelines/` `serving/` `modelzoo/` + uv workspace (`examlops-workspace`) | shipped |
| 16 | Pipeline ops CLI (`exa pipeline deploy/export-registry/scaffold`) + dashboard Pipelines page + ScaffoldWizard | shipped |
| 17 | Observability: Alertmanager + OpenTelemetry tracing to Grafana Tempo + React Flow architecture diagram | shipped |
| 18 | Agent core: `exa_agent/` package, ~34 tools/9 groups, `interrupt()` confirm-before-write, SQLite checkpointer | shipped |

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
make agent                  # start LangGraph management agent (platform/services/agent/)

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

### Remote Server n1 (137.204.56.169)

Same ports as local — e.g. http://137.204.56.169:18099 for the Dashboard.

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
    │   ├── agent/              # LangGraph management agent + exa_agent/ package
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
- `.github/workflows/deploy.yml` — deploys to server `n1` on merge to main
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
