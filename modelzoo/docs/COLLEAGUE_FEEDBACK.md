# Code Review Feedback for Francesco
_Prepared by Mohsen | 2026-03-22_

Hi Francesco,

While setting up the CI pipeline I ran automated code quality checks (ruff linter, ruff formatter,
mypy type checker) across the whole codebase. Below is a list of issues found in your files.
None of these break functionality right now, but they will cause the CI lint job to report
warnings on every push. It would be great if you could address them when you have time.

I have not touched your files — all changes are yours to make.

---

## 1. Formatting (ruff format)

The following files need to be reformatted to match the project's code style (Black-compatible).
You can fix all of them at once by running:

```bash
ruff format seanergys_modelzoo
```

**Files that need reformatting:**
- `seanergys_modelzoo/datasets/common/seanergys_dataset.py`
- `seanergys_modelzoo/datasets/f_data.py`
- `seanergys_modelzoo/datasets/pm100.py`
- `seanergys_modelzoo/models/common/seanergys_model.py`
- `seanergys_modelzoo/models/tasks/performance_prediction/mack/mack_model.py`
- `seanergys_modelzoo/models/tasks/performance_prediction/mcbound/mcbound_model.py`
- `seanergys_modelzoo/models/tasks/power_consumption_prediction/jpcp/jpcp_model.py`

---

## 2. Unused Imports (ruff check)

These imports are defined but never used in the file. They add noise and slow down imports.
You can auto-fix most of them by running:

```bash
ruff check --fix seanergys_modelzoo
```

| File | Unused import | How to fix |
|------|--------------|------------|
| `seanergys_dataset.py` | `field_validator` from pydantic | Remove it from the import line |
| `f_data.py` | `os`, `Any`, `Iterable`, `SeanergysLogger` | Remove them from their import lines |
| `seanergys_model.py` | `Literal`, `pickle`, `SeanergysModelConfiguration` | Remove them from their import lines |
| `mack_model.py` | `pickle`, `Iterable`, `SeanergysModelParams` | Remove them from their import lines |
| `mcbound_model.py` | `SeanergysModelParams`, `SeanergysDataloader`, `SeanergysModel` | Remove them from their import lines |
| `jpcp_model.py` | `time`, `Callable`, `Iterable`, `Type`, `SeanergysModel` | Remove them from their import lines |

---

## 3. Duplicate Method Definition in `mack_model.py`

**File:** `seanergys_modelzoo/models/tasks/performance_prediction/mack/mack_model.py`

`transform_embeddings` is defined **twice** in the `MACK` class — once at line 69 and again
at line 143. Python silently uses only the second definition, which means the first one
(line 69) is completely ignored. This is almost certainly a bug.

```python
# Line 69 — this version is silently ignored
def transform_embeddings(self, features: np.ndarray) -> np.ndarray:
    ...

# Line 143 — this is the one Python actually uses
def transform_embeddings(self, features: np.ndarray, encoder_weights: str = "...") -> np.ndarray:
    ...
```

**What to do:** Decide which version is correct and remove the other one. If both are needed,
give them different names.

---

## 4. Duplicate Import in `mcbound_model.py`

**File:** `seanergys_modelzoo/models/tasks/performance_prediction/mcbound/mcbound_model.py`

`Enum` is imported twice:

```python
from enum import Enum   # line 7
...
from enum import Enum   # line 12 — duplicate, remove this one
```

**What to do:** Remove the second `from enum import Enum` line.

---

## 5. f-strings Without Placeholders

Several log calls use an `f"..."` string but have no `{variable}` inside them. This is
harmless but pointless — the `f` prefix does nothing.

```python
# Wrong — f prefix does nothing here
model.logger.info(f"Training on FData")

# Correct
model.logger.info("Training on FData")
```

**Files affected:**
- `mack_model.py` — lines 166, 225
- `mcbound_model.py` — line 141
- `jpcp_model.py` — lines 118, 139, 170, 191

Auto-fixable with:

```bash
ruff check --fix seanergys_modelzoo
```

---

## 6. Design Issues (not caught by linter — needs discussion)

These are not linter errors but design gaps that will cause problems in CI and for other
developers using your code.

### 6.1 `get_dummy_params()` returns `None` — **this blocks CI tests for your datasets**

**Files:** `seanergys_modelzoo/datasets/common/seanergys_dataset.py`,
`seanergys_modelzoo/models/common/seanergys_model.py`

Both base classes define `get_dummy_params()` as:

```python
@classmethod
def get_dummy_params(cls) -> dict:
    pass  # returns None implicitly
```

This means any code that calls `get_dummy_params()` expecting a dict will silently get `None`
and crash later in a confusing way. It should either raise `NotImplementedError` (forcing
subclasses to implement it) or return a sensible default.

The CI smoke tests and integration tests are now fully dynamic — they discover every dataset
class and test it automatically. **If `get_dummy_params()` returns None, those tests will
skip with a message.** The test will never cover your dataset until you implement it.

Each concrete dataset class must implement it, for example:

```python
# In FDataDataset:
@classmethod
def get_dummy_params(cls) -> dict:
    return {
        "input_features": ["nnuma", "duration"],
        "output_features": ["avgpcon"],
    }

# In PM100Dataset:
@classmethod
def get_dummy_params(cls) -> dict:
    return {
        "input_features": ["num_nodes_req", "run_time"],
        "output_features": ["num_nodes_alloc"],
    }
```

The base class should raise `NotImplementedError` instead of silently returning None:

```python
@classmethod
def get_dummy_params(cls) -> dict:
    raise NotImplementedError(
        f"{cls.__name__} must implement get_dummy_params() "
        "to be automatically tested in CI."
    )
```

### 6.2 `get_train_config()` and `get_test_config()` return type hints are wrong — **CRITICAL: was breaking all integration tests**

**Files:** `mack_model.py`, `jpcp_model.py`, `mcbound_model.py`
**Fixed by Mohsen on branch `ci/add-gitlab-pipeline`.**

Both `get_train_config()` and `get_test_config()` were annotated as `Dict[Tuple[...]]`.
`Dict` requires **two** type parameters (key type and value type). In Python 3.12 this
raises a `TypeError` at import time — before any test even starts:

```
TypeError: Too few arguments for typing.Dict; actual 1, expected 2
```

Because the error happened when the module was first imported, `retrieve_instances_from_file`
failed for every model file. The result: `MODEL_CLASSES` in `tests/conftest.py` was always
empty, and all integration tests were silently skipped with `[NOTSET]` — no model was ever
tested in CI.

The fix was adding `str,` as the key type in all six methods (3 files × 2 methods):

```python
# Before — caused TypeError at import time in Python 3.12
def get_train_config(cls) -> Dict[Tuple["MACK", SeanergysDataset, ...]]:
def get_test_config(cls, ...)  -> Dict[Tuple[SeanergysModel, ...]]:

# After — correct
def get_train_config(cls) -> Dict[str, Tuple["MACK", SeanergysDataset, ...]]:
def get_test_config(cls, ...) -> Dict[str, Tuple[SeanergysModel, ...]]:
```

**For future models:** always use `Dict[str, Tuple[...]]` — the string key is the config
entry name (e.g. `"fdata"`, `"pm100"`) that you already use inside the method body.

### 6.3 `is_dummy=True` has two bugs in `FDataDataset` — **breaks sample data creation**

**File:** `f_data.py`

Running `make sample-data` revealed two bugs in the `is_dummy=True` block:

**Bug 1 — wrong file name (hyphen vs underscore):**
```python
# Wrong — "21-04" does not exist in AVAILABLE_FILES
self.files = "21-04"

# Correct
self.files = "21_04"
```

**Bug 2 — invalid filter format:**
```python
# Wrong — appends a bare tuple to a list-of-lists, creating mixed format
# If filters = [[("adt", ">=", "2023-12-01"), ...]], after += it becomes:
# [[("adt", ">=", "2023-12-01"), ...], ("adt", "<=", "2021-04-02")]
# pyarrow sees the bare tuple and raises: "a" is not a valid operator
self.filters += [("adt", "<=", "2021-04-02")]

# Correct — wrap in a list to match the list-of-lists format
self.filters += [[ ("adt", "<=", "2021-04-02") ]]
```

Beyond these bugs, `is_dummy=True` still downloads from Zenodo with filters — it does not
generate synthetic data in memory. The intended purpose of `is_dummy` in CI is:
*"load a tiny amount of data without needing the real dataset or network."*
The cleanest fix is to generate a small synthetic DataFrame:

```python
if self.is_dummy:
    # No file, no network — tiny synthetic data for CI
    self._df = pd.DataFrame({
        "nnuma": [1, 2, 3, 4, 5],
        "duration": [10.0, 20.0, 30.0, 40.0, 50.0],
        "avgpcon": [100.0, 200.0, 300.0, 400.0, 500.0],
    })
    return
```

### 6.6 `MACK.get_train_config()` — `mem_compute_bound` does not exist in raw F-DATA files

**File:** `mack_model.py`

MACK declares `output_features=["mem_compute_bound"]` in its `get_train_config()`.
However, `mem_compute_bound` is **not a column in the raw F-DATA parquet files** — it is
absent from all 38 monthly files (verified on `24_04.parquet`, the most recent).

The raw F-DATA schema contains `opint` (operational intensity), `flops`, `mbwidth`, and
`pclass` (a computed kmeans cluster label), but no `mem_compute_bound`.

This means:
- `make sample-data` creates `sample_fdata.parquet` with all 45 raw columns — correct
- The integration test then tries to load the fixture with `output_features=["mem_compute_bound"]`
  and fails: `No match for FieldRef.Name(mem_compute_bound)`

**What to do:**
Either change `output_features` to a column that actually exists in the raw data
(e.g. `"pclass"`, `"avgpcon"`), or add `mem_compute_bound` as a preprocessing step
that derives it from `opint` before the dataset is loaded.

### 6.5 `JPCP.get_train_config()` — wrong tuple format for PM100 entry

**File:** `jpcp_model.py`, line 157

The PM100 entry does not follow the `(model, dataset_cls, (ds_params, dl_params))` contract.
Francesco is instantiating `PM100Dataset` directly with positional arguments (which Pydantic
does not support) instead of passing the class and params separately:

```python
# Wrong — PM100Dataset is a Pydantic BaseModel, does not accept positional args
# Also breaks the (model, dataset_cls, (ds_params, dl_params)) contract
config_dict["pm100"] = (model_pm100, PM100Dataset(pm100_config, data_loader_pm100))

# Correct — pass the class, not an instance
config_dict["pm100"] = (model_pm100, PM100Dataset, (pm100_config, data_loader_pm100))
```

This causes `JPCP.get_train_config()` to raise `BaseModel.__init__() takes 1 positional
argument but 3 were given`, so the PM100 dataset is never included in CI testing for JPCP.

### 6.4 `get_train_config()` hardcodes `use_zenodo_url=True`

**Files:** `mack_model.py`, `jpcp_model.py`, `mcbound_model.py`

All training configs hardcode `use_zenodo_url=True`, which means CI always tries to
download from the internet. Consider accepting a `data_path` parameter override so that
CI can point to a local fixture file:

```python
@classmethod
def get_train_config(cls, data_path: Optional[Path] = None) -> dict:
    fdata_config = SeanergysDatasetParams(
        use_zenodo_url=data_path is None,
        data_path=data_path,
        ...
    )
```

---

## Summary

| # | Type | Files | Auto-fixable? |
|---|------|-------|--------------|
| 1 | Formatting | 7 files in `seanergys_modelzoo/` | Yes — `ruff format seanergys_modelzoo` |
| 2 | Unused imports | 6 files | Yes — `ruff check --fix seanergys_modelzoo` |
| 3 | Duplicate method | `mack_model.py` | Manual — decide which version to keep |
| 4 | Duplicate import | `mcbound_model.py` | Yes — `ruff check --fix seanergys_modelzoo` |
| 5 | f-strings | `mack_model.py`, `mcbound_model.py`, `jpcp_model.py` | Yes — `ruff check --fix seanergys_modelzoo` |
| 6.1 | `get_dummy_params()` returns None | base classes | Manual |
| 6.2 | ~~**CRITICAL** Wrong type hint on `get_train_config()` / `get_test_config()` — broke all integration tests~~ **Fixed by Mohsen** | model files | Done |
| 6.3 | `is_dummy=True` has two bugs (wrong filename, invalid filter format) + still needs network | `f_data.py` | Manual |
| 6.4 | `use_zenodo_url=True` hardcoded | model files | Manual |
| 6.5 | **JPCP** `get_train_config()` PM100 entry uses wrong tuple format + positional Pydantic args | `jpcp_model.py` line 157 | Manual (one line fix) |
| 6.6 | **MACK** `output_features=["mem_compute_bound"]` — column does not exist in raw F-DATA files | `mack_model.py` | Manual — use an existing column or derive it in preprocessing |

**Quick wins** (run these two commands and items 1, 2, 4, 5 are done):
```bash
ruff format seanergys_modelzoo
ruff check --fix seanergys_modelzoo
```

Items 3 and 6.x need manual attention. Happy to discuss any of these.
