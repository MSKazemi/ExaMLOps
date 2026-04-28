# Pipelines

ML pipelines for ExaMLOps. The auto-pipeline discovers all registered models and runs train → evaluate → MLflow log → promote for every model × dataset combination.

## Running the Auto-Pipeline

From the repo root:

```bash
# 1. Start infrastructure
make dev-up

# 2. Install dependencies
make install

# 3. List all registered models and datasets
make pipeline-list

# 4. Dry run (no Zenodo download, fast)
make pipeline-run

# 5. Full run (downloads real data from Zenodo)
make pipeline-run-full

# 6. Single model + dataset
.venv/bin/python pipelines/pipeline_generator.py --model JPCP --dataset PM100Dataset --dummy
```

## Adding a New Model

1. Create `pipelines/model_configs/<model>_config.py` implementing `SeanergysModelConfiguration` with `get_train_components()` and `get_inference_params()`
2. Set `MODEL_CLASS = <ModelClass>` on the config class

That's all — the new model appears in all pipelines automatically.

## Pipeline Structure

```
pipelines/
├── pipeline_generator.py   # Auto-pipeline: discovers all registered models
└── model_configs/
    └── jpcp_config.py      # JPCPConfiguration (JPCP × PM100Dataset, FDataDataset)
```

## Results

View logged experiments and registered models at [http://localhost:5000](http://localhost:5000).

Experiment names follow the pattern `{model}_{dataset}` (e.g. `jpcp_pm100dataset`).
