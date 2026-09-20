# Use-Case Packs

A **use-case pack** is one deployment's content that runs *on top of* the ExaMLOps platform —
its models, datasets, per-model YAML, and transform shims. Packs are kept **separate from the
platform** (ADR 0094): the platform core (`platform/`), the pipeline engine (`pipelines/`), and the
serving substrate (`serving/`) are use-case-agnostic and name no concrete model or dataset. A pack
plugs in through the loader seam, so a different user brings a different pack without editing any
platform code.

## Selecting the active pack

The platform resolves the active pack from the environment:

```
EXAMLOPS_USECASE_DIR=/path/to/pack     # pack root (default: usecases/reference)
RAY_MODELS_DIR / MODELS_YAML_DIR       # explicit per-model YAML dir (override)
```

## Anatomy of a pack

```
usecases/<name>/
  pack.toml                 # manifest: models dir, config package, datasets, framework binding
  models/*.yaml             # per-model declarative config (single source of truth)
  model_configs/*.py        # Python transform shims (model-bound callables)
  datasets/schemas.json     # optional: dataset field schemas (for dataset cards)
```

`pack.toml` declares everything the engine needs so it imports nothing use-case-specific:

```toml
[content]
models_dir = "models"
config_package = "model_configs"

[framework]                 # the ML framework this pack's models build on ("module:attr")
model_base  = "my_modellib.base:Model"
config_base = "my_modellib.base:ModelConfiguration"
# … pipeline_step, model_params, dataloader(_params), get_backend, framework_adapter,
#    model_task, tasks_root

[datasets]                  # dataset name (as used in per-model YAML) -> "module:Class"
MyData = "my_modellib.datasets.mydata:MyDataset"
```

## Guardrail

`platform/ci/check_usecase_boundary.py` (run in CI + `tests/unit/test_usecase_boundary.py`) fails if
the platform core or the pipeline engine imports a use-case model library directly. The seam —
`pipelines.usecase` (engine) and `examlops.usecase` (CLI) — is the only bridge.

See `design/adr/0094-platform-vs-usecase-separation.md`.
