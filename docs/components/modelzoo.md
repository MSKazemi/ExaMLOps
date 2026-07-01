# ModelZoo — Model Library

The ModelZoo (`modelzoo` package) is the model library. It defines the abstract model interface, the sklearn base class, datasets, and the `@pipeline_step` decorator that the orchestration layer hooks into.

## Package structure

```
modelzoo/modelzoo/
├── models/
│   ├── common/
│   │   ├── dataplane_model.py          # Abstract base — DataplaneModel
│   │   ├── sklearn_dataplane_model.py  # Concrete base — DataplaneSklearnModel
│   │   └── dataplane_configurator.py   # Pipeline config — DataplaneModelConfiguration
│   └── tasks/
│       ├── power_consumption_prediction/
│       │   └── jpcp/jpcp_model.py      # JPCP model (sklearn)
│       └── performance_prediction/
│           ├── mack/mack_model.py      # MACK model
│           └── mcbound/mcbound_model.py
├── datasets/
│   └── common/dataplane_dataset.py     # Dataset base class
├── dataloader/
│   └── dataplane_dataloader.py         # Dataloader wrapping datasets
└── decorators.py                       # @pipeline_step decorator
```

## The model interface

All models inherit from `DataplaneModel` (Pydantic v2 `BaseModel` + `ABC`). The key abstract methods:

```python
class DataplaneModel(BaseModel, ABC):
    task_type: DataplaneModelTask   # REGRESSION or CLASSIFICATION

    @abstractmethod
    def build_model(self) -> None: ...

    @abstractmethod
    def train(self, train_loader, val_loader=None, ...) -> dict: ...

    @abstractmethod
    def predict(self, data_loader, ...) -> Iterable: ...

    @abstractmethod
    def evaluate(self, data_loader, metrics=None, ...) -> list: ...

    @abstractmethod
    def save(self, path, ...) -> bool: ...

    @classmethod
    @abstractmethod
    def load(cls, path, ...) -> DataplaneModel: ...
```

Pipeline-facing steps (`train_step`, `evaluate_step`, `predict_step`) are already implemented on the base class using the `@pipeline_step` decorator. Subclasses only need to implement the abstract ML methods.

## DataplaneSklearnModel

For sklearn-based models, subclass `DataplaneSklearnModel` instead:

```python
from modelzoo.models.common.sklearn_dataplane_model import DataplaneSklearnModel

class MyModel(DataplaneSklearnModel):
    task_type: DataplaneModelTask = DataplaneModelTask.REGRESSION

    def build_model(self):
        from sklearn.ensemble import GradientBoostingRegressor
        self.estimator = GradientBoostingRegressor(n_estimators=100)

    def train(self, train_loader, val_loader=None, **kwargs):
        X, y = self._extract_data_from_loader(train_loader)
        self.estimator.fit(X, y)
        self.is_trained = True
        return {"train_samples": len(y)}
```

`DataplaneSklearnModel` provides `self.estimator`, `_extract_data_from_loader()`, and default `predict`, `evaluate`, `save`, `load` implementations.

## The @pipeline_step decorator

`@pipeline_step` marks a method so the pipeline generator can discover and wrap it into a Prefect task:

```python
from modelzoo.decorators import pipeline_step

@pipeline_step(name="train_step", retries=1)
def train_step(self, train_loader, val_loader=None):
    ...
```

The decorator is framework-agnostic — it adds `_is_pipeline_step`, `_step_name`, and `_step_retries` attributes to the method. No Prefect import in the model library.

## Model configuration

Every model needs a matching Python shim in `pipelines/model_configs/`. In Phase 14 the shim is a **plain class** (not inheriting `DataplaneModelConfiguration`) — it provides only the callables that cannot be expressed in YAML. All declarative config (datasets, features, lifecycle, serving, prefect) lives in `pipelines/models/<name>.yaml`.

```python
# pipelines/model_configs/mymodel_config.py
from typing import ClassVar
from my_model import MyModel
from my_dataset import MyDataset

class MyModelConfiguration:
    """Python shim — provides transform callables only.
    All declarative config lives in pipelines/models/mymodel.yaml.
    """
    MODEL_CLASS: ClassVar[type] = MyModel
    SUPPORTED_DATASETS: ClassVar[list] = [MyDataset]

    @classmethod
    def resolve_embedding_type(cls, value: str):
        return MyEmbedding[value]

    @classmethod
    def get_transforms(cls, model, dataset_cls: type) -> dict:
        return {"transform": model.embedding_parsing}
```

## Adding a new model (step by step)

**Step 1** — Create the model file:

```
modelzoo/modelzoo/models/tasks/my_task/my_model/my_model.py
```

Implement `DataplaneSklearnModel` (or `DataplaneModel` for non-sklearn frameworks).

**Step 2** — Create the YAML config file: `pipelines/models/<name_lower>.yaml` with datasets, lifecycle, serving, prefect, and inference sections (generated automatically by `exa scaffold`).

**Step 3** — Create the transforms shim (generated automatically): `pipelines/model_configs/<name>_config.py` — plain class with `MODEL_CLASS`, `SUPPORTED_DATASETS`, `resolve_embedding_type`, `get_transforms` (model-bound callables that cannot be expressed in YAML).

**Step 4** — Verify discovery:

```bash
exa pipeline validate
# Should show: MyModel × [MyDataset]
```

**Step 5** — Run the pipeline:

```bash
exa pipeline run --model MyModel --dummy
```

No changes to `pipeline_generator.py` required.

## Existing models

| Model | Task | Datasets |
|---|---|---|
| `JPCP` | Power consumption prediction | PM100Dataset |
| `MACK` | Performance prediction | FData |
| `MCBound` | Performance prediction | ScriptAI |

## ModelZoo environment

The ModelZoo uses a separate **Poetry** environment:

```bash
# Install ModelZoo deps
cd modelzoo && poetry install

# Run ModelZoo tests
make modelzoo-test
```

The package is installed into the root `.venv` as `modelzoo` so the pipeline generator can import it directly.

## Control Plane integration (Phase 12)

The Control Plane tracks ModelZoo repository freshness by recording push events and maintaining a per-model `is_stale` flag in SQLite. The ModelZoo package itself is unchanged — the integration is entirely in the control plane and the `exa` CLI.

### How freshness is tracked

Every time the ModelZoo repo receives a push to `MODELZOO_WATCH_BRANCH` (via webhook or background poller), the control plane:

1. Inserts a row into `modelzoo_events` with the commit SHA, branch, pushed-by, and source (`webhook` / `poll`).
2. Upserts a row in `model_freshness` for **every model in the auto-discovery registry**, setting `is_stale=1` and recording `stale_since`.
3. Optionally triggers `POST /retrain` for each model if `auto_retrain` is enabled.

When a model is retrained (via approval or auto-retrain), its `model_freshness.last_retrain_commit` is updated to match the latest ModelZoo commit and `is_stale` is cleared to `0`.

### Freshness status values

| Status | Meaning |
|---|---|
| `current` | `last_retrain_commit == latest_modelzoo_commit` |
| `stale` | A new push was recorded that hasn't been retrained yet |
| `unknown` | No freshness data exists yet (model never trained since Phase 12 was deployed) |

### CLI

```bash
exa modelzoo status               # table: model, CURRENT/STALE, stale since, latest commit
exa --json modelzoo status        # machine-readable JSON
exa modelzoo events               # push event history
exa modelzoo sync                 # trigger one GitLab poll cycle manually
exa modelzoo config               # show runtime config (auto_retrain, poll_interval, branch)
```

### Dashboard

The **Models** page shows a `CURRENT` (green) or `UPDATED` (amber) freshness badge next to each model card, derived from the `GET /modelzoo/status` endpoint. An event feed at the bottom of the page lists recent push events (commit SHA, pushed by, timestamp, source).

The **Config** page → ModelZoo Integration section shows the pre-filled webhook URL and a toggle for auto-retrain.

## Per-model YAML config (Phase 14)

Every model has a complete config file at `pipelines/models/<name>.yaml`. This is the single source of truth for:
- Datasets, input/output features, filter splits
- Lifecycle thresholds (Staging → Canary → Production)
- Prefect schedule, work pool, concurrency limit
- Ray Serve aliases and MLflow model_id
- Inference input/output schema

The pipeline generator scans `pipelines/models/*.yaml` at startup — no Python class scanning.

```bash
# Validate all YAML files against Python shims
exa pipeline validate

# Run with env overlay (tighter thresholds, different backend)
exa pipeline run --env prod
```

Per-environment overlays in `pipelines/envs/<env>.yaml` deep-merge on top of the per-model YAML so the same codebase can serve different environments with one `ENV=` variable.
