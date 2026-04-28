# ExaMLOps

MLOps platform for HPC power prediction — built on the [seanergys-modelzoo](https://gitlab.com/ecs-lab/sustainablehpc/seanergys-modelzoo) framework.

## Quick Start

```bash
make help       # list all available targets
make bootstrap  # one-shot: start infra + install deps
```

## Running the Auto-Pipeline

The pipeline auto-discovers all registered models and runs train → evaluate → MLflow log → promote for every model × dataset combination.

**Step 1 — start infrastructure** (MLflow · Postgres · Prefect):
```bash
make dev-up
```

**Step 2 — install dependencies:**
```bash
make install
```

**Step 3 — list registered models:**
```bash
make pipeline-list
# Registered models:
#   JPCP: ['PM100Dataset', 'FDataDataset']
```

**Step 4 — dry run with dummy data** (fast, no Zenodo download):
```bash
make pipeline-run
```

**Step 5 — full run with real data** (downloads from Zenodo):
```bash
make pipeline-run-full
```

**Step 6 — view results in MLflow UI:**
```
http://localhost:5000
```

**Run a single model/dataset:**
```bash
.venv/bin/python pipelines/pipeline_generator.py --model JPCP --dataset PM100Dataset --dummy
```

## Service URLs

| Service | URL |
|---|---|
| MLflow UI | http://localhost:5000 |
| Prefect UI | http://localhost:4200 |
| Ray Serve API | http://localhost:8001 |
| Ray Dashboard | http://localhost:8265 |

## Adding a New Model

1. Create `pipelines/model_configs/<model>_config.py` implementing `SeanergysModelConfiguration` with `get_train_components()` and `get_inference_params()`
2. Set `MODEL_CLASS = <ModelClass>` on the config — the pipeline discovers and runs it automatically.

## Key Make Targets

```bash
make dev-up             # start MLflow + Postgres + Prefect + Ray Serve
make dev-down           # stop all containers
make pipeline-run       # run auto-pipeline (dummy data)
make pipeline-run-full  # run auto-pipeline (real data from Zenodo)
make pipeline-list      # list registered models
make check              # lint + typecheck + test
make status             # show running services
```

## Repository Layout

```
ExaMLOps/
├── docs/                    # Documentation
│   ├── 10-architecture/     # Architecture & requirements
│   ├── 20-roadmap/          # Roadmap & steps
│   ├── 30-infrastructure/   # Dev environment, Docker
│   └── 40-pipelines/        # Pipeline docs
├── infra/
│   ├── docker-compose/      # MLflow + Postgres (dev)
│   └── slurm-adapter/       # HPC/Slurm integration (mock + real)
├── modelzoo/                # seanergys-modelzoo model library
├── pipelines/
│   ├── pipeline_generator.py   # Auto-pipeline orchestration
│   └── model_configs/          # One config per model
└── services/
    └── ray_serving/         # Multi-model Ray Serve inference API
```

## Documentation

- [Dev Environment](docs/30-infrastructure/dev-environment.md)
- [Architecture Overview](docs/10-architecture/mlops-architecture-overview.md)
- [UC Power Prediction Pipeline](docs/40-pipelines/uc-power-prediction.md)
