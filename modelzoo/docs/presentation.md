# CI/CD & Testing in SEANERGYS ModelZoo
**Simple and practical approach for reliable ML pipelines**

Mohsen Seyedkazemi Ardebili · SEANERGYS · 2026

> "I will explain how we use testing and CI/CD in our SEANERGYS project —
> from the basics to our actual implementation."

---

## Agenda

1. Why do we need testing? — the problem
2. What is a test?
3. Types of tests — the testing pyramid
4. What is CI/CD and why we need it?
5. CI/CD for team collaboration
6. CI & testing in ML / MLOps
7. Our GitLab CI pipeline — how it works
8. Tools: pytest, ruff, mypy, Poetry
9. Walk-through: unit → smoke → integration
10. Q & A

---

## 1. Why Do We Need Testing?

**In a team, one person's change can silently break another person's work — with no error message.**

Without tests, the only way to catch this is to manually run everything after every change — which no one does consistently. Tests make this automatic.

---

**Example 1 — Silent bug: `is_trained` never set**

Someone refactors `train()` but forgets to set `self.is_trained = True`.
Training completes, no crash. But the model is not actually ready.
Our test catches it immediately:

```python
# tests/integration/test_model_train.py
history = model_instance.train(train_data_loader=loader)

assert model_instance.is_trained is True   # fails if someone broke train()
assert history is not None
assert "training" in history
```

---

**Example 2 — Silent bug: dataset has no features defined**

Someone adds a new dataset but forgets to declare `input_features` and `output_features`.
No crash — but the model trains on the wrong columns.
Our smoke test catches it:

```python
# tests/smoke/test_datasets_smoke.py
def test_dataset_contract_via_get_dummy_params(dataset_cls):
    params = dataset_cls.get_dummy_params()
    assert "input_features" in params
    assert "output_features" in params
    assert len(params["input_features"]) >= 1    # must not be empty
    assert len(params["output_features"]) >= 1
```

---

**Example 3 — Broken save/load: you deploy a different model than you trained**

`save()` returns `True` but `load()` gives back a broken object.
No error at save time — only discovered when the model is used in production.
Our integration test catches it:

```python
# tests/integration/test_model_pipeline.py
saved = model_instance.save(str(save_path / "model"))
assert saved

loaded_model = type(model_instance).load(str(save_path / "model"))
assert loaded_model is not None
```

> "In ML, a bug does not always crash your code — it just gives you wrong results silently."

---

## 2. What is a Test?

**A test is code that calls your code and checks the result is correct.**

```python
# Without a test — you run the model and hope:
model_instance.train(train_data_loader=loader)   # did it work? no idea

# With a test — you know immediately:
# from tests/integration/test_model_train.py
history = model_instance.train(train_data_loader=loader)

assert model_instance.is_trained is True
assert history is not None
assert "training" in history

predictions = model_instance.predict(loader)
assert len(predictions) == len(dataset)
```

**Why do we write tests?**

- **Catch bugs early** — find problems at commit time, not in production
- **Document behaviour** — tests show exactly how code is meant to be used
- **Enable refactoring** — change internals safely; tests tell you if you broke something
- **Enforce contracts** — every model must have train/predict/save/load — tests verify it

> "A test is the only proof that your code does what you think it does."

---

## 3. Types of Tests — The Testing Pyramid

```
         ┌─────────────────────────┐
         │   Integration Tests     │  few · slow · most realistic
         │  train→predict→save→load│
    ┌────┴─────────────────────────┴────┐
    │         Smoke Tests               │  some · medium speed · system-level
    │  can it start? does it discover?  │
┌───┴───────────────────────────────────┴───┐
│               Unit Tests                  │  many · fast · cheap · the foundation
│       fast, isolated, no I/O              │
└───────────────────────────────────────────┘
```

| Type        | Speed   | Scope                      | Example in ModelZoo                        |
|-------------|---------|----------------------------|--------------------------------------------|
| Unit        | < 1 s   | One function / class       | Does `__len__` return the right count?     |
| Smoke       | ~10 s   | Whole system, no real data | Can all models be discovered + instantiated? |
| Integration | ~5 min  | Full pipeline, real-ish data | train → predict → save → load             |

> "We test at different levels to ensure reliability at every layer."

---

## 4. What is CI/CD and Why We Need It?

**CI = Continuous Integration** — every commit triggers an automated pipeline.
**CD = Continuous Delivery** — validated artefacts are deployed automatically.

| Without CI | With CI |
|------------|---------|
| Bugs discovered days/weeks after commit | Every push validated in minutes |
| "Works on my machine" syndrome | Consistent environment via Docker |
| Merging is stressful and infrequent | Small, safe, frequent integrations |
| Manual test runs — easy to skip | Tests run automatically — no excuses |
| Broken main branch blocks everyone | Broken code never reaches main |

> "CI acts as a **gatekeeper** — code cannot be merged if tests fail."

> *"If it hurts, do it more often" — Martin Fowler*

---

## 5. CI/CD for Team Collaboration

**CI/CD is not only for testing — it is a team safety system.**

- Every team member follows the **same quality rules**
- No one can accidentally break what someone else built
- New team members can contribute safely from day one
- Code reviews focus on logic, not style (ruff + mypy handle that)

**In SEANERGYS:**

- Multiple people add models and datasets
- CI automatically tests every new model — **zero extra work**
- If your model breaks the pipeline, you know before it reaches main

> "CI/CD enables the team to move fast without breaking things."

---

## 6. CI & Testing in ML / MLOps

**ML adds unique challenges beyond classical software:**

| Challenge | Why it matters |
|-----------|----------------|
| **Data is code** | Datasets must be validated — schema, dtypes, feature lists |
| **Model artefacts** | Saved/loaded models must produce identical predictions |
| **Reproducibility** | Same data + same config = same model, every time |
| **Heavy dependencies** | PyTorch, scikit-learn, XGBoost — must be pinned and tested |
| **No network in CI** | Tests must not depend on Zenodo, databases, or remote files |

> "Traditional software testing is not enough — we need data contracts and model behaviour checks."

---

## 7. Our GitLab CI Pipeline

**The pipeline is defined in one file: `.gitlab-ci.yml`**

```yaml
stages:
  - sanity
  - validate
  - lint
  - test
```

**Key idea:** each stage is a faster, cheaper filter.
If syntax is broken, stop after 30 seconds — don't waste 5 minutes installing PyTorch.

```
Push Code
    ↓
SANITY (~30s) → VALIDATE (~10s) → LINT (~15s) → TEST (~5min)
    ↓
Safe & Reliable Code
```

> "If one stage fails, the pipeline stops immediately."

---

### How a Job Works

Every stage contains jobs. A job looks like this:

```yaml
sanity:python-syntax:       # job name
  stage: sanity             # which stage it belongs to
  image: python:3.12-slim   # Docker environment (clean, reproducible)
  script:                   # commands to run
    - find seanergys_modelzoo ci tests -name "*.py" -print0 \
      | xargs -0 -r python -m py_compile
  allow_failure: false      # this must pass
```

| Field           | Meaning                              |
|-----------------|--------------------------------------|
| `stage`         | Which stage this job belongs to      |
| `image`         | Docker container — clean environment |
| `script`        | Commands to execute                  |
| `allow_failure` | If true, failure is advisory only    |

> "Each job runs in a clean Docker container — no local environment issues."

---

### Stage 1: Sanity — Basic Checks

Three jobs, zero dependencies, runs in < 30 seconds:

```yaml
sanity:python-syntax:           # catches SyntaxError before any install
  script:
    - python -m py_compile seanergys_modelzoo/...

sanity:check-structure:         # ensures expected folders exist
  script:
    - test -f pyproject.toml
    - test -d seanergys_modelzoo && test -d tests

sanity:check-secrets:           # scans for accidental credentials
  script:
    - ./ci/check_secrets.sh .
  allow_failure: true           # advisory — never blocks
```

---

### Stage 3: Lint — Code Quality

```yaml
lint:python:                    # catches unused imports, bad patterns
  script:
    - ruff check seanergys_modelzoo ci tests

lint:format:                    # enforces consistent style
  script:
    - ruff format --check seanergys_modelzoo ci tests

lint:mypy:                      # catches type errors before runtime
  script:
    - mypy seanergys_modelzoo ci tests --ignore-missing-imports
```

All lint jobs have `allow_failure: true` — they are advisory, not blockers.

---

### Stage 4: Test Jobs

| Job                   | What it checks                             |
|-----------------------|--------------------------------------------|
| `test:unit`           | Pure logic, no I/O — fast                  |
| `test:smoke`          | Imports, discovery, model instantiation    |
| `test:smoke-models`   | All models instantiate with minimal config |
| `test:smoke-datasets` | Dataset contracts (features defined)       |
| `test:integration`    | Full pipeline: train→predict→save→load     |
| `test:import-check`   | Package is importable                      |
| `test:audit`          | CVE scan on all dependencies (advisory)    |

---

## 8. Tools & Libraries

| Tool      | Role                  | Why we chose it                                              |
|-----------|-----------------------|--------------------------------------------------------------|
| **pytest**    | Test runner           | Fixtures, parametrize, marks. `importorskip` for optional deps |
| **ruff**      | Linter + formatter    | Rust-powered — replaces flake8 + isort. Sub-second feedback  |
| **mypy**      | Static type checker   | Catches type errors before runtime — especially useful in ML |
| **Poetry**    | Dependency manager    | Lockfile = reproducible installs. Groups: dev / ci / core    |
| **pydantic**  | Data validation       | Dataset/dataloader params validated at construction time     |
| **pip-audit** | Security scan         | Checks all dependencies for known CVEs                       |

> "We use pytest for testing and GitLab CI for automation.
> ruff and mypy catch code quality issues before the tests even run."

---

## 9. Walk-through: Unit → Smoke → Integration

### Unit Test

```python
# tests/unit/test_dataset_contract.py

def test_dataset_len_and_getitem():
    from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset

    class SyntheticDataset(SeanergysDataset):
        def _load_data_impl(self):
            self._samples = [([i*1.0, i*2.0], i) for i in range(3)]
        def __getitem__(self, idx): return self._samples[idx]
        def __len__(self):          return len(self._samples)

    ds = SyntheticDataset()
    ds._load_data_impl()

    assert len(ds) == 3
    x, y = ds[0]
    assert x == [0.0, 0.0] and y == 0
```

- No external data — synthetic samples created inline
- Tests the **contract** (interface), not the implementation
- `pytest.importorskip("torch")` skips cleanly if torch is absent

---

### Smoke Test — Auto-Discovery (Key Design)

Auto-discovery is defined once in `tests/conftest.py` and shared across all tests:

```python
# tests/conftest.py

def _collect_model_classes() -> Dict[str, Type]:
    from ci.utils import retrieve_instances_from_file
    from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel

    classes: Dict[str, Type] = {}
    for py_file in (_PROJECT_ROOT / "seanergys_modelzoo" / "models" / "tasks").rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            classes.update(retrieve_instances_from_file(py_file, SeanergysModel))
        except Exception:
            continue
    return classes

# Collected once at session start — used to parametrize all tests
MODEL_CLASSES   = _collect_model_classes()
DATASET_CLASSES = _collect_dataset_classes()
```

**Why this is powerful:**
- No hardcoding — models are **discovered automatically** at test session start
- Add a new model file → it is automatically included in smoke + integration tests
- Zero CI maintenance as the project grows

> "New models are tested automatically — no one needs to update the test."

---

### Integration Test — Full Pipeline

```python
# tests/integration/test_model_pipeline.py

@pytest.mark.parametrize("model_cls", list(MODEL_CLASSES.values()))
def test_full_pipeline(model_cls, tmp_path):
    config = model_cls.get_train_config()

    for entry_name, (model_instance, dataset_cls, (ds_params, dl_params)) in config.items():
        ds_params.is_dummy = True       

        dataset = dataset_cls.from_config(ds_params)
        dataset.load_data()

        loader = SeanergysDataloader.from_config(dl_params, dataset=dataset)

        history = model_instance.train(loader)
        assert model_instance.is_trained
        assert history is not None

        predictions = model_instance.predict(loader)
        assert len(predictions) > 0

        assert model_instance.save(str(tmp_path / "model"))
        assert type(model_instance).load(str(tmp_path / "model")) is not None
```

- `is_dummy=True` — no network needed, uses in-memory synthetic data
- `@pytest.mark.parametrize` — one test covers all models automatically
- Skips gracefully if a dataset does not support `is_dummy` yet

---

### Handling Data in CI — No Network Allowed

| Strategy | How | When |
|----------|-----|------|
| `is_dummy=True` | Dataset generates synthetic samples in memory | Default for all CI |
| Fixture files | `tests/fixtures/sample_data/*.parquet` — `make sample-data` | Integration with real-ish data |
| `@zenodo` mark | Real download, skipped with `CI_SKIP_ZENODO=1` | Optional, network tests |

---

## Summary

**What the CI pipeline guarantees on every push:**

| Stage       | Guarantee                                                              |
|-------------|------------------------------------------------------------------------|
| SANITY      | Syntax correct · Repo structure intact · No obvious secrets            |
| VALIDATE    | All YAML/JSON config files parse correctly                             |
| LINT        | Style consistent · Types checked — advisory                           |
| UNIT        | Dataset contract · Logger · Metadata · Configurator · Dataloader params |
| SMOKE       | Package imports · Model discovery · Dataset contracts · Instantiation  |
| INTEGRATION | Every model: train → predict → save → load (offline, auto-discovered)  |
| AUDIT       | No known CVEs in production dependencies — advisory                    |

**Three key ideas:**
- **Testing** → correctness
- **CI/CD** → automation + team safety
- **Auto-discovery** → scalability (zero maintenance as project grows)

> "Reliable ML requires automated testing — and our pipeline delivers it on every commit."

---

## Q & A

**References:**
- Martin Fowler — Continuous Integration (martinfowler.com)
- pytest docs — pytest.org
- ruff — github.com/astral-sh/ruff
- GitLab CI/CD — docs.gitlab.com/ee/ci
- MLOps principles — ml-ops.org


```python
def test_dataset_discovery():
    """
    PASS: at least one SeanergysDataset subclass found in datasets/.
    FAIL: no dataset classes found — check that your dataset file is in
          seanergys_modelzoo/datasets/ and subclasses SeanergysDataset.
    """
    assert len(DATASET_CLASSES) >= 1, (
        "No SeanergysDataset subclasses found in seanergys_modelzoo/datasets/.\n"
        "Make sure your dataset file:\n"
        "  - lives in seanergys_modelzoo/datasets/\n"
        "  - subclasses SeanergysDataset\n"
        "  - is not named __init__.py"
    )


def test_model_discovery():
    """
    PASS: at least one SeanergysModel subclass found in models/tasks/.
    FAIL: no model classes found — check that your model file is in
          seanergys_modelzoo/models/tasks/ and subclasses SeanergysModel.
    """
    assert len(MODEL_CLASSES) >= 1, (
        "No SeanergysModel subclasses found in seanergys_modelzoo/models/tasks/.\n"
        "Make sure your model file:\n"
        "  - lives in seanergys_modelzoo/models/tasks/<task>/<name>/\n"
        "  - subclasses SeanergysModel\n"
        "  - is not named __init__.py"
    )
```


```python
@pytest.mark.zenodo
@pytest.mark.parametrize(
    "dataset_name,config",
    list(_DATASET_CONFIGS_FROM_MODELS.items()),
    ids=list(_DATASET_CONFIGS_FROM_MODELS.keys()),
)
def test_dataset_loads_approach_b(dataset_name, config):
    """
    Load dataset using params from model get_train_config() + is_dummy=True.

    PASS: dataset instantiates with model-provided params, loads data, len > 0,
          dataset[0] returns a valid (x, y) tuple.
    SKIP: CI_SKIP_ZENODO=1 set, or no model references this dataset.
    FAIL: dataset loads 0 rows, or dataset[0] fails.

    Marked @zenodo because Francesco's is_dummy=True downloads from Zenodo
    with filters.
    Skip with: CI_SKIP_ZENODO=1 make test-smoke
    """
    if os.environ.get("CI_SKIP_ZENODO") == "1":
        pytest.skip("SKIP: CI_SKIP_ZENODO=1 — Zenodo tests disabled for this run.")

    dataset_cls, ds_params, source = config

    params = ds_params.model_dump()
    params["is_dummy"] = True

    try:
        dataset = dataset_cls(**params)
    except Exception as e:
        pytest.fail(
            f"FAIL [{dataset_name}] Failed to instantiate using params "
            f"from {source}.\n  → Error: {e}"
        )

    assert len(dataset) >= 1, (
        f"FAIL [{dataset_name}] Loaded 0 samples.\n"
        f"  → Params came from {source}.\n"
        f"  → Check that is_dummy=True reduces data but still returns at least 1 row."
    )

    item = dataset[0]
    assert item is not None, (
        f"FAIL [{dataset_name}] Dataset[0] returned None.\n"
        f"  → Check that {dataset_name}.__getitem__() returns a valid (x, y) tuple."
    )
```


```python
def test_dataloader_params_to_dict():
    """SeanergysDataloaderParams serializes to dict."""
    from seanergys_modelzoo.models.common.seanergys_configurator import (
        SeanergysDataloaderParams,
    )

    params = SeanergysDataloaderParams(batch_size=8, drop_last=True)
    d = params.to_dict()
    assert isinstance(d, dict)
    assert d.get("batch_size") == 8
    assert d.get("drop_last") is True
```

```python
def add(a, b):
    return a + b
```


```python
def test_add():
    assert add(1, 2) == 3
```