# CI & Testing: (For Meeting) 


**Scope:** This document covers **both** general software CI (lint, format, syntax) **and** ML-specific CI (models, datasets, training, inference, dataloader). 

---

## Part 1: What Is CI and Why Do We Care?

**CI = Continuous Integration.** When you push code to GitLab, the pipeline runs automatically. It checks if the code compiles, passes tests, follows style rules, and has no obvious problems.

**Benefits:**
- Catch bugs early before they reach production
- Everyone works with the same checks
- New contributors get fast feedback
- Why we need CI and ML CI and in which phase of the project we use them?
| Phase of Development | Typical CI Action | Purpose |
|---|---|---|
| Development | Run tests (unit tests) | Verify that individual functions and modules work correctly |
| Development | Check code quality (lint, formatting, static analysis) | Ensure code follows standards and avoid common programming errors |
| Integration | Run integration tests | Verify that multiple components work together correctly |
| Build | Build packages / containers | Ensure the software can be compiled or packaged successfully |
| Security | Run security checks (dependency scan, vulnerability scan) | Detect insecure libraries or potential security issues |
| Pre-release / Deployment | Deploy artifacts (binaries, containers, packages) | Prepare or release the software for staging or production environments |
---

## Part 1.5: Two Kinds of CI — General vs ML

Our pipeline mixes **two types of checks**. Different team members may care more about one than the other:

| Type | What it checks | Who cares most |
|------|----------------|----------------|
| **General CI (software)** | Syntax, structure, lint, format, type check, secrets, YAML validation, dependency audit | Code quality and consistency |
| **ML CI (machine learning)** | Models, datasets, dataloader, training code, inference code — do they work correctly together? | Colleagues developing models |

**We need both.** General CI keeps the codebase healthy. ML CI checks that the models and data pipelines built by different developers actually train, predict, and integrate correctly.

**Summary:**

- **General CI:** Sanity, validate, lint stages + test:unit (utils, configs), test:import-check, test:audit
- **ML CI:** test:validate-models, test:validate-datasets, test:integration (train, predict, save/load), smoke tests on models/datasets

| Job | General CI | ML CI |
|-----|------------|-------|
| sanity:* | ✓ | |
| validate:yaml-json | ✓ | |
| lint:* | ✓ | |
| test:unit | ✓ | (partly: configurator, metadata) |
| test:smoke | | ✓ (model/dataset discovery, instantiation) |
| test:import-check | ✓ | |
| test:validate-models | | ✓ |
| test:validate-datasets | | ✓ |
| test:integration | | ✓ (train, predict, dataloader, dataset) |
| test:audit | ✓ | |

---

## Part 2: Our Pipeline — Stages in Order

Our pipeline has **4 stages**, run in this order:

```
sanity → validate → lint → test
```

Each stage can have several **jobs**. Jobs in the same stage run in parallel. The next stage starts only when all jobs in the current stage succeed (unless `allow_failure: true`).

---

## Part 3: What Each Stage Does (Simple Explanation)

### Stage 1: Sanity — **General CI**

| Job | What it does | In plain words — what it means, how it works, what data |
|-----|--------------|--------------------------------------------------------|
| **sanity:python-syntax** | Compiles all Python files | **Meaning:** Python code has no syntax errors. **How:** Try to compile each `.py` file. **Data:** None. |
| **sanity:check-structure** | Checks that key files/dirs exist | **Meaning:** Repo has the expected layout. **How:** Check `pyproject.toml`, `seanergys_modelzoo/`, `ci/`, `tests/` exist. **Data:** None. |
| **sanity:check-secrets** | Scans for hardcoded secrets | **Meaning:** No API keys or tokens in code. **How:** Search for patterns like `api_key="..."`, `secret_key=`. **Data:** None. |

**Runs:** Always (on every push)

---

### Stage 2: Validate — **General CI**

| Job | What it does | In plain words — what it means, how it works, what data |
|-----|--------------|--------------------------------------------------------|
| **validate:yaml-json** | Validates all `.yml`, `.yaml`, `.json` files | **Meaning:** Config files can be read and parsed. **How:** Load each YAML/JSON file and check it doesn't crash. **Data:** The config files in the repo. |

**Runs:** When YAML/JSON changes, or always (depends on rules)

---

### Stage 3: Lint — **General CI**

| Job | What it does | In plain words — what it means, how it works, what data |
|-----|--------------|--------------------------------------------------------|
| **lint:python** | Runs `ruff check` | **Meaning:** Code follows style rules, no unused imports. **How:** Ruff scans Python files and reports issues. **Data:** None. |
| **lint:format** | Runs `ruff format --check` | **Meaning:** Code is formatted consistently (spaces, line length). **How:** Check if files match the expected format. **Data:** None. |
| **lint:mypy** | Runs mypy (type checker) | **Meaning:** Type hints are correct. **How:** Mypy checks that types match. **Data:** None. |

**Runs:** When Python files change

**Note:** These jobs have `allow_failure: true` — if they fail, the pipeline still passes. We can change this later when we fix all issues.

---

### Stage 4: Test — **General CI + ML CI**

| Job | What it does | In plain words — what it means, how it works, what data |
|-----|--------------|--------------------------------------------------------|
| **test:unit** | Runs `pytest tests/unit/` (utils, configs, metadata, dataset contract) | **Meaning:** Check that small pieces of code (configs, logger, metadata) work on their own. **How:** Run small, fast tests. **Data:** None for most; `test_dataset_contract` uses a minimal in-memory synthetic dataset to test the abstract contract only. |
| **test:smoke** | Runs `pytest tests/smoke/` — model/dataset discovery, instantiation | **Meaning:** Quick check that we can find all models/datasets and create them. **How:** Scan the code folders, import each model/dataset class, create it with minimal settings. **Data:** Smoke: discovery only. Load tests use local fixtures (`sample_fdata.parquet`, `sample_pm100.parquet`) when present. |
| **test:import-check** | Imports core module | **Meaning:** The package can be imported without errors. **How:** Run `from seanergys_modelzoo... import ...`. **Data:** None. |
| **test:validate-models** | Model smoke + `ci/validate_models.py` | **Meaning:** Every model added by a developer can be found and passes a quick run. **How:** (1) Scan model folders, (2) Call `run_dummy_validation()` on each model — a short training run. **Data:** Real data (Zenodo or fixtures). "Dummy" = minimal run, not fake data. |
| **test:validate-datasets** | Dataset smoke + `ci/validate_datasets.py` | **Meaning:** Every dataset added by a developer can be found. **How:** Scan dataset folders, find all dataset classes. Does not load data. **Data:** None (discovery only). |
| **test:integration** | Sample data + `pytest tests/integration/` | **Meaning:** Model + dataset + dataloader work together: train, predict, save, load. **How:** Load small fixture parquet files, train model for a few steps, predict, save and load. **Data:** Small chunks of real data in `tests/fixtures/sample_data/` (e.g. `sample_fdata.parquet`, `sample_pm100.parquet` — ~100 rows from Zenodo). Create with `make sample-data`. |
| **test:audit** | `pip-audit` for dependency vulnerabilities | **Meaning:** Our dependencies have no known security issues. **How:** Compare our package list to a known-vulnerability database. **Data:** None. |

**Why ML tests matter :** When developer A adds a new model, and developer B adds a dataset, the ML CI checks that:
- The model instantiates and trains
- The dataloader and dataset load data correctly
- Training and inference code run without errors
- Save/load works for deployment

**Runs:** Depends on rules (see below)

---

## Part 4: GitLab CI — All the Options Explained

When you edit `.gitlab-ci.yml`, you use **rules** and other keywords. Here is what each means.

### `rules` — when does a job run?

A job runs if **at least one rule matches**. Rules are evaluated top to bottom; the **first match** wins.

### Rule options (inside each rule)

| Option | Meaning | Example |
|--------|---------|---------|
| **`if`** | Run only if this condition is true | `if: $CI_PIPELINE_SOURCE == "merge_request_event"` |
| **`changes`** | Run only when these files change | `changes: ["**/*.py"]` |
| **`exists`** | Run only if these files exist | `exists: ["Dockerfile"]` |
| **`when`** | When to run the job | `when: on_success`, `manual`, `never`, `delayed` |
| **`allow_failure`** | If true, pipeline passes even if job fails | `allow_failure: true` |
| **`variables`** | Set variables when rule matches | `variables: { ENV: "test" }` |

### `when` values

| Value | Meaning |
|-------|---------|
| `on_success` | Run when previous stage succeeds (default) |
| `on_failure` | Run only when a previous job failed |
| `always` | Run every time |
| `manual` | Requires someone to click "Play" in GitLab UI |
| `never` | Never run (useful to disable a job) |
| `delayed` | Run after a delay (needs `start_in`) |

### `allow_failure` (at job level)

| Value | Effect |
|-------|--------|
| `false` | Job failure = pipeline failure (default) |
| `true` | Job can fail; pipeline still passes (e.g. optional checks) |

### Common `if` conditions

| Expression | Meaning |
|------------|---------|
| `$CI_PIPELINE_SOURCE == "push"` | Run on direct push |
| `$CI_PIPELINE_SOURCE == "merge_request_event"` | Run on merge request |
| `$CI_COMMIT_BRANCH == "main"` | Run only on main branch |
| `$CI_COMMIT_TAG` | Run only when pushing a tag |

### Example rule combinations

```yaml
# Example 1: Run only when .py files change
rules:
  - changes: ["**/*.py"]

# Example 2: Run on MR or when models change, or always
rules:
  - changes: ["seanergys_modelzoo/models/**", "ci/**"]
  - when: always

# Example 3: Run manually on main only
rules:
  - if: $CI_COMMIT_BRANCH == "main"
    when: manual
    allow_failure: true

# Example 4: Never run (disabled)
rules:
  - when: never
```

---

## Part 5: Our Current Rules Summary

| Job | Rules | Allow failure |
|-----|-------|---------------|
| sanity:* | (none = always) | python-syntax, structure: No; check-secrets: Yes |
| validate:yaml-json | changes in yml/yaml/json OR always | No |
| lint:* | changes in \*\*/*.py | Yes |
| test:unit, test:smoke | changes in \*\*/*.py | No |
| test:import-check | (always) | No |
| test:validate-models | changes in models/ or ci/, or always | Yes |
| test:validate-datasets | changes in datasets/ or ci/ | Yes |
| test:integration | changes in models/, datasets/, tests/integration/, scripts/ | No |
| test:audit | changes in pyproject.toml, poetry.lock | Yes |

---

## Part 6: Packaging (Poetry)

We use **Poetry** to manage dependencies and build the package.

- **`pyproject.toml`** — project metadata, dependencies, dev/ci groups
- **`poetry.lock`** — locked versions (everyone gets same versions)
- **Install:** `poetry install` (prod) or `poetry install --with dev,ci` (dev + tests)

**Publishing to GitLab Package Registry (future):** We can build a wheel and push it so other projects can `pip install seanergys-modelzoo` from our GitLab.

---

## Part 7: What Tests We Have vs. What We Could Add

### General CI tests (software quality)

| Category | Tests | Location |
|----------|-------|----------|
| Unit | Utils, logger, configurator, metadata, dataloader params | `tests/unit/` |
| Import | Core package imports | test:import-check |
| Audit | Dependency vulnerabilities | test:audit |

### ML CI tests (what colleagues care about: models, datasets, train, inference)

| Category | What it validates | In plain words — what it means, how it works, what data | Location |
|----------|-------------------|--------------------------------------------------------|----------|
| **Smoke (models)** | Every model can be found and built | **Meaning:** We can discover and instantiate each model. **How:** Scan `models/`, import each model class, create it with minimal config (e.g. `n_estimators=2`). **Data:** None — we don't train, just build the object. | `test_models_smoke.py` |
| **Smoke (datasets)** | Every dataset can be found and loaded | **Meaning:** We can discover datasets and load them from local fixtures or Zenodo. **How:** Scan `datasets/`, load FData/PM100 from fixture parquet files. **Data:** Local fixtures (`tests/fixtures/sample_data/sample_fdata.parquet`, `sample_pm100.parquet`) — small chunks of real data (~100 rows). Create with `make sample-data`. Zenodo optional (skipped if `CI_SKIP_ZENODO=1`). | `test_datasets_smoke.py`, `test_datasets_load.py` |
| **Integration (train)** | Training code runs | **Meaning:** Model trains for a few steps without error. **How:** Load fixture data → create dataloader → call `model.train()`. **Data:** Fixture parquet files from `tests/fixtures/sample_data/`. | `test_model_train.py` |
| **Integration (predict)** | Inference code runs | **Meaning:** After training, we can run predictions. **How:** Call `model.predict(dataloader)`. **Data:** Same fixture data as train. | Same |
| **Integration (save/load)** | Saved model can be loaded | **Meaning:** Model saves to disk and loads back correctly. **How:** `model.save(path)` → `Model.load(path)` → predict again, compare outputs. **Data:** Same fixture data. | `test_model_save_load.py` |
| **Unit (dataset contract)** | `__len__`, `__getitem__` work | **Meaning:** Dataset behaves like a standard dataset (length, indexing). **How:** Create a minimal in-memory subclass with a few hardcoded samples, call `len(ds)`, `ds[i]`. **Data:** Only this test uses synthetic (no file) — to validate the abstract contract without needing real data. | `test_dataset_contract.py` |

**ML tests check:** When a developer adds a model or dataset, does the training code, inference code, dataloader, and dataset work correctly together?

### Tests we could add (gaps)

| Gap | Description | General / ML | Effort |
|-----|-------------|--------------|--------|
| **Pydantic validation tests** | Invalid input → ValidationError | General | Low |
| **Pydantic schema tests** | model_json_schema() for config | General | Low |
| **Per-model integration** | One test per (model, dataset) pair — validate each dev's model works | **ML** | Medium |
| **Dataloader integration** | Explicit test that dataloader + dataset produce correct batches | **ML** | Low |
| **Documentation build** | Docs build (mkdocs/sphinx) | General | Low |
| **E2E (future)** | train → MLflow → serve | **ML** | When MLOps exists |

---

## Part 8: Using Pydantic for Tests

We use **Pydantic** for data structures:

- `SeanergysModelMetadata` — model metadata (name, version, metrics, etc.)
- `SeanergysDataset` — base dataset with `data_path`, `metadata`, etc.
- `SeanergysModel` — base model class
- `FDataDataset`, `PM100Dataset` — use `Field`, `model_validator`
- Config classes in `seanergys_configurator`

### How to use Pydantic in tests

1. **Validation tests** — pass invalid data, expect `ValidationError`:

```python
def test_metadata_rejects_empty_name():
    from pydantic import ValidationError
    from seanergys_modelzoo.models.common.seanergys_model_metadata import SeanergysModelMetadata

    with pytest.raises(ValidationError):
        SeanergysModelMetadata()  # name is required
```

2. **Round-trip tests** — already in `test_metadata.py` (model_dump → from_dict).

3. **Config from JSON** — load config, parse with Pydantic, assert fields.

4. **model_validator tests** — for FData, PM100, etc., test that validators work (e.g. path must exist, or month format is valid).

### Where to put Pydantic tests (CI step: test:unit)

| Aspect | Value |
|--------|-------|
| **CI job** | test:unit |
| **Stage** | 4 (Test) |
| **Location** | `tests/unit/` |

**File organization — two options:**

| Option | Approach | Files |
|--------|----------|-------|
| **A** | Add to existing test files | `test_metadata.py` (SeanergysModelMetadata validation), `test_configurator.py` (config classes validation) |
| **B** | Create a dedicated file | `test_pydantic_validation.py` — all Pydantic validation/schema tests in one place |

Both are valid. Option A keeps tests next to the model they cover; Option B centralises Pydantic tests.

---

## Part 9: Next Steps (Decisions to Make)

### General CI decisions

| # | Topic | Options | Question for team |
|---|-------|---------|-------------------|
| 1 | **Lint allow_failure** | Keep Yes or change to No | Should lint/format/mypy block the pipeline? |
| 2 | **test:import-check rules** | Currently always runs | Should it run only on .py changes to save time? |
| 3 | **Add Pydantic validation tests** | Yes/No | Add tests for invalid input → ValidationError? Option A (existing files) or B (dedicated file)? |
| 4 | **Add docs build job** | Yes/No | Check that documentation builds? |
| 5 | **Publish to GitLab Package Registry** | When ready | Enable so MLOps can `pip install` from GitLab? |

### ML CI decisions

| # | Topic | Options | Question for team |
|---|-------|---------|-------------------|
| 6 | **test:validate-models/datasets allow_failure** | Keep Yes or change to No | Should model/dataset validation failures block the pipeline? |
| 7 | **Per-model integration tests** | Yes/No | Add one integration test per (model, dataset) pair? |
| 8 | **Dataloader + dataset test** | Yes/No | Add explicit test that dataloader produces correct batches? |

### Shared

| # | Topic | Options | Question for team |
|---|-------|---------|-------------------|
| 9 | **Poetry extras** | Add [torch], [sklearn], [all] | For models with different deps (see MODELZOO_ARCHITECTURE_NOTES.md) |

**See also Part 11** for a full codebase review and concrete fixes (Python version, Zenodo skip, ci deps, etc.).

---

## Part 10: Quick Reference

### Run locally (Makefile)

```
make sample-data      # Create fixture data first (small parquet chunks from Zenodo)
make test             # Unit + smoke
make test-integration # Full integration (uses fixtures)
make lint             # Ruff check
make format           # Ruff format
make audit            # Security scan
```

### Fixtures (small chunks of real data)

Tests use **fixtures** — small parquet files (~100 rows) derived from Zenodo. Create with `make sample-data`. Location: `tests/fixtures/sample_data/` (`sample_fdata.parquet`, `sample_pm100.parquet`). See `tests/fixtures/README.md`.

### Key files

| File | Purpose |
|------|---------|
| `.gitlab-ci.yml` | Pipeline definition |
| `pyproject.toml` | Dependencies, pytest config |
| `ci/validate_*.py` | Validation scripts |
| `tests/conftest.py` | Pytest fixtures |
| `tests/fixtures/README.md` | Fixture usage and creation |

### Document structure

| Part | Content |
|------|---------|
| 1–3 | CI basics, General vs ML, stages |
| 4–5 | GitLab rules, current rules summary |
| 6–8 | Packaging, tests, Pydantic usage |
| 9 | Decisions to make |
| 10 | Quick reference |
| **11** | **Codebase review — gaps and recommended fixes** |

---

## Part 11: Codebase Review — What We Forgot or Should Implement

*Based on a full review of the code, CI, and tests. Add these to the backlog.*

### Critical / High priority

| # | Item | Problem | Fix |
|---|------|---------|-----|
| 1 | **Python version mismatch** | `pyproject.toml` requires `>=3.12,<3.15` but CI uses `python:3.11-slim` | Use `python:3.12-slim` in `.gitlab-ci.yml` for all jobs |
| 2 | **test:smoke may run Zenodo tests** | CI runs `pytest tests/smoke/ -v` without excluding Zenodo. Zenodo tests need network and can fail | Add `CI_SKIP_ZENODO=1` to test:smoke, or `-m "not zenodo"` so CI uses local fixtures only |
| 3 | **test:unit and test:smoke missing ci deps** | Both use `poetry install --with dev` only. Model/dataset discovery and test_dataset_contract need torch, sklearn | Use `--with dev,ci` for test:unit and test:smoke so all tests run (not skip) |
| 4 | **test:import-check too narrow** | Only imports `SeanergysLogger` | Also import core models/datasets or run `poetry run python -c "import seanergys_modelzoo"` |

### Important (ML-focused)

| # | Item | Problem | Fix |
|---|------|---------|-----|
| 5 | **Model scripts validation** | README: "check che ci siano i files training.py, testing.py predict.py". `validate_models.py` does not check that model scripts exist | Add script: for each model folder, check that `scripts/train_*.py` and `scripts/inference_*.py` (or similar) exist |
| 6 | **test:integration rules too narrow** | Runs only when models/, datasets/, tests/integration/, scripts/ change | Add `seanergys_modelzoo/dataloader/**`, `seanergys_modelzoo/models/common/**` so dataloader/common model changes trigger integration |
| 7 | **No dataloader integration test** | Dataloader + dataset batch production not explicitly tested | Add `tests/integration/test_dataloader.py` or similar: dataloader yields correct batch shape from dataset |
| 8 | **ScriptAI dataset not in load tests** | `test_datasets_load.py` covers FData and PM100 only | Add ScriptAI load test if the dataset is used, or document why it is excluded |
| 9 | **validate_datasets only discovers** | Unlike `validate_models` (runs `run_dummy_validation`), datasets are only discovered, not loaded | Consider adding a minimal load check per dataset (or rely on test_datasets_load) |

### Nice to have (general CI)

| # | Item | Problem | Fix |
|---|------|---------|-----|
| 10 | **Pydantic ValidationError tests** | Invalid input not tested | Add tests that bad input raises `ValidationError` (e.g. metadata without `name`) |
| 11 | **sanity:check-structure** | Could check more | Add `test -d seanergys_modelzoo/models`, `test -d seanergys_modelzoo/datasets` |
| 12 | **Coverage reporting** | No coverage in CI | Add `pytest --cov=seanergys_modelzoo` and optional coverage report (or badge) |
| 13 | **Makefile install** | `make install` uses `--with dev` only | Document that full tests need `poetry install --with dev,ci` or add `make install-ci` |
| 14 | **test:audit rules** | Runs only on pyproject.toml, poetry.lock changes | Consider `when: always` so we periodically re-check even when deps don’t change |

### Already implemented (no action)

| Item | Status |
|------|--------|
| sanity checks structure (pyproject.toml, dirs) | OK |
| validate_models runs run_dummy_validation | OK |
| test:integration creates sample-data before run | OK |
| conftest fixtures (fdata_dataset, etc.) | OK |
| Zenodo skip via CI_SKIP_ZENODO in test code | OK (but CI doesn’t set it) |

### Summary: quick wins

1. Change CI image to `python:3.12-slim`
2. Add `CI_SKIP_ZENODO=1` to test:smoke job
3. Add `--with ci` to test:unit and test:smoke `poetry install`
4. Broaden test:integration rules to include dataloader and models/common

---

## Part 12: What `make test-smoke` Does — Output Explained

### Command

```
make test-smoke
```

Runs:

```
.venv/bin/pytest tests/smoke/ -v -m "not zenodo"
```

- **`tests/smoke/`** — All tests under the smoke directory
- **`-v`** — Verbose output (one line per test)
- **`-m "not zenodo"`** — Exclude tests marked `@pytest.mark.zenodo` (those require network to fetch from Zenodo)

### Output interpretation (example run)

| Output | Meaning |
|--------|---------|
| `collected 9 items` | Pytest found 9 tests in `tests/smoke/` |
| `2 deselected` | 2 tests are skipped because they have `@pytest.mark.zenodo` and we ran with `-m "not zenodo"` |
| `7 selected` | 7 tests actually run (no network needed) |
| `7 passed` | All 7 tests passed |

### The 7 tests that run

| # | Test | What it checks |
|---|------|----------------|
| 1 | `test_load_fdata_from_local_fixtures` | Load F-DATA dataset from `sample_fdata.parquet` (skips if fixture missing) |
| 2 | `test_load_pm100_from_local_fixtures` | Load PM100 dataset from `sample_pm100.parquet` (skips if fixture missing) |
| 3 | `test_dataset_discovery` | Discover all `SeanergysDataset` subclasses in `seanergys_modelzoo/datasets/` |
| 4 | `test_logger_imports` | `SeanergysLogger` imports and instantiates |
| 5 | `test_ci_utils_imports` | `ci.utils` discovery helpers are importable |
| 6 | `test_model_discovery` | Discover all `SeanergysModel` subclasses in `seanergys_modelzoo/models/` |
| 7 | `test_model_instantiation` | Each discovered model can be instantiated with minimal config (no training) |

**Prerequisites:** Run `make sample-data` first so `sample_fdata.parquet` and `sample_pm100.parquet` exist in `tests/fixtures/sample_data/`. Otherwise the two load tests skip (or fail depending on logic).

### The 2 deselected tests (Zenodo)

- `test_load_fdata_from_zenodo` — Fetches F-DATA from Zenodo URL
- `test_load_pm100_from_zenodo` — Fetches PM100 from Zenodo URL

To run these (requires network): `make test-smoke-zenodo` or `pytest tests/smoke/ -v` (without `-m "not zenodo"`).

### PytestUnknownMarkWarning

```
Unknown pytest.mark.zenodo - is this a typo?
```

The `zenodo` mark is used but not registered. To silence the warning, add to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = [
    "zenodo: marks tests that require Zenodo network access (deselect with -m 'not zenodo')",
]
```

---

