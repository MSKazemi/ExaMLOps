# Add a new model in 5 minutes

ExaMLOps discovers models by scanning `pipelines/models/*.yaml` — one YAML file per model is the single source of truth. Phase 2 added a one-command scaffold; Phase 14 extended it to also generate the per-model YAML.

## TL;DR

```bash
exa scaffold DemoAD --task anomaly_detection --type classification

# verify auto-discovery picked it up
exa pipeline validate

# run the full HPC pipeline against dummy data
exa pipeline run --model DemoAD --dummy

# run the model-specific tests + the registry-integrity guard
.venv/bin/pytest tests/unit/test_demoad.py tests/unit/test_registry_integrity.py -v
```

## Alternatively: `exa scaffold` CLI or dashboard wizard

**Via the `exa` CLI** (no Makefile needed):

```bash
exa scaffold DemoAD                                                # defaults: performance_prediction / regression
exa scaffold DemoAD --task anomaly_detection --type classification
exa scaffold DemoAD --force                                        # overwrite existing files
```

`--task` and `--type` are enum-constrained: `exa scaffold --help` shows valid choices in `[a|b|c]` format and tab-completes them after running `exa --install-completion`. Run from the repo root — it invokes `tools/scaffold_model.py` directly.

**Via the dashboard** (browser-based, admin only):

1. Log in as admin at http://localhost:18099
2. Go to **Models → New Model**
3. Fill in the name, task type, and promotion thresholds in the wizard
4. Preview the generated files before confirming
5. Click **Create model** to write the files to the repo (requires the dashboard container to be started with the repo bind mount — set automatically in `docker-compose.yml`)

After either method, the same verification steps apply:

```bash
exa pipeline validate
exa pipeline run --model DemoAD --dummy
.venv/bin/pytest tests/unit/test_demoad.py tests/unit/test_registry_integrity.py -v
```

## What gets generated

Four files (all under git, none gitignored):

| File | Purpose |
|---|---|
| `modelzoo/modelzoo/models/tasks/<task>/<name>/<name>_model.py` | `DataplaneSklearnModel` subclass — replace the placeholder estimator with your real algorithm |
| `pipelines/models/<name_lower>.yaml` | Full model config — datasets, features, lifecycle thresholds, Prefect schedule, serving aliases, inference schema, **DataPlane UUID** |
| `pipelines/model_configs/<name>_config.py` | Transforms-only Python shim — provides `MODEL_CLASS`, `SUPPORTED_DATASETS`, `resolve_embedding_type`, `get_transforms` (model-bound callables that cannot be expressed in YAML) |
| `tests/unit/test_<name>.py` | Four checks: registered, config loads, inference contract, instantiation |

A matching `__init__.py` is created next to the model file so Python treats the
new directory as a package — required for the auto-discovery scan.

> **DataPlane UUID:** The scaffold auto-generates a `dataplane_uuid` field in the model YAML. The DataPlane bridge registers a dedicated req/res handler for this model at that UUID on startup. Share the UUID with HPC teams via `exa dataplane list` or the dashboard `/dataplane` page.

## Available options

```text
exa scaffold
  NAME=<PascalCase>          # required (e.g. DemoAD, MyClassifier, FuguTransformer)
  TASK=<task>                # default: performance_prediction
                             # one of: performance_prediction, power_consumption_prediction, anomaly_detection
  TASK_TYPE=<type>           # default: classification
                             # one of: classification, regression
  FORCE=1                    # overwrite existing files
```

The full set of knobs (estimator class, promotion metric / threshold /
direction, etc.) is exposed via `python tools/scaffold_model.py --help` if you
need to override them — the Makefile target uses the defaults from
`tools/model_template/cookiecutter.json`.

## After scaffolding

1. **Implement the algorithm.** Replace the placeholder `model_class` and any
   feature handling in `<name>_model.py`. The scaffold uses
   `RandomForestClassifier` (classification) by default; substitute in your
   real estimator and update `_inject_defaults`.
2. **Tune lifecycle thresholds.** Edit `pipelines/models/<name_lower>.yaml` — set
   `lifecycle[*].threshold` values for Staging, Canary, and Production. The
   scaffold uses 80%/90%/100% of your `--promotion-threshold` as starting points.
3. **Verify the YAML guard.** `exa pipeline validate` runs all YAML
   integrity checks in under 5 seconds and catches missing fields, invalid
   lifecycle directions, and shim-YAML dataset mismatches.

That's it — the seven-step HPC training flow, MLflow registration, the
promotion gate, and Ray Serve auto-loading all kick in for the new model
without further wiring.
