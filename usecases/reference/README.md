# reference Use-Case Pack

The reference HPC use case — the models, datasets, and pipelines that run **on top of** ExaMLOps.
This is *content*, kept separate from the platform (ADR 0094); the platform loads it through the
`pipelines.usecase` / `examlops.usecase` seam and names none of it directly.

## Contents

| Path | What |
|---|---|
| `pack.toml` | Manifest: per-model YAML dir, config package, dataset registry, and the `seanergys_modelzoo` framework binding. |
| `models/*.yaml` | Per-model declarative config for `JPCP`, `MACK`, `MCBound` (single source of truth). |
| `model_configs/*.py` | Python transform shims (model-bound callables that can't be expressed in YAML). |
| `features/*.yaml` | Feature-view definitions (ADR 0017): the one train/serve definition of the models' input features, enforced by the training feature gate and applied by serving's `FeatureTransformer`. |
| `datasets/schemas.json` | Field schemas for the pack's datasets (e.g. `FData`), used to build Croissant dataset cards. |

The model *implementations* and dataset classes live upstream in `modelzoo/`
(`seanergys_modelzoo`), which `pack.toml` binds via `pythonpath = ["../../modelzoo"]`.

## Using it

It is the default pack, so `EXAMLOPS_USECASE_DIR` defaults to `usecases/reference`. Everything works
unchanged:

```bash
exa pipeline list
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
exa dataplane-bus list
exa cards dataset FData          # schema comes from datasets/schemas.json
```

## Adding a model

1. `exa scaffold <Name> --task <task> --type <type>` (writes into this pack).
2. Add the model implementation upstream in `modelzoo/`.
3. Edit `models/<name>.yaml` (declarative config) + `model_configs/<name>_config.py` (transforms).
4. `tests/unit/test_registry_integrity.py` guards half-applied scaffolding.
