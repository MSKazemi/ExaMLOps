#!/usr/bin/env python3
"""
Create small sample fixture files for local testing and CI.

WHY THIS FILE EXISTS
--------------------
Francesco's original CI scripts ran validation directly against Zenodo —
every test run downloaded the full dataset. That made CI slow, fragile
(network-dependent), and impossible to run offline.

This script solves that by downloading a tiny sample ONCE (during CI setup or
locally with `make sample-data`) and saving it as a local parquet file.
The integration tests then use those local files — no network needed during
the actual test run.

HOW IT WORKS
------------
Instead of hardcoding dataset names or requiring get_dummy_params(), this script
uses the models as the entry point:

  1. Auto-discover every SeanergysModel subclass in models/tasks/
  2. Call get_train_config() on each — this already defines which dataset class
     to use, which input/output features, and which filters
  3. Override ds_params with is_dummy=True to get a small filtered subset
  4. Instantiate the dataset — the model_validator fires load_data() automatically
  5. Save the loaded DataFrame to tests/fixtures/sample_data/

Datasets are deduplicated — if MACK and MCBound both use FDataDataset, it is
only downloaded once.

When a developer adds a new model with get_train_config(), its dataset fixture
is created automatically — no changes needed here.

LIMITATION
----------
If a dataset is added without any model (no get_train_config() pointing to it),
this script will not create a fixture for it. In that case, get_dummy_params()
must be implemented on the dataset class — see docs/COLLEAGUE_FEEDBACK.md §6.1.

Usage:
    python scripts/create_sample_data.py
    SAMPLE_MAX_ROWS=50 python scripts/create_sample_data.py
    python scripts/create_sample_data.py --max-rows 200

Output:
    tests/fixtures/sample_data/sample_<datasetclassname_lower>.parquet
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _max_rows() -> int:
    return int(os.environ.get("SAMPLE_MAX_ROWS", os.environ.get("CI_MAX_SAMPLES", "100")))


def _output_dir() -> Path:
    d = PROJECT_ROOT / "tests" / "fixtures" / "sample_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _discover_model_classes() -> dict:
    """
    Auto-discover all concrete SeanergysModel subclasses in models/tasks/.
    Same mechanism as tests/conftest.py — no model names hardcoded.
    """
    try:
        from ci.utils import retrieve_instances_from_file
        from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel
    except ImportError as e:
        print(f"Cannot import project packages: {e}", file=sys.stderr)
        sys.exit(1)

    classes = {}
    for py_file in (PROJECT_ROOT / "seanergys_modelzoo" / "models" / "tasks").rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            classes.update(retrieve_instances_from_file(py_file, SeanergysModel))
        except Exception:
            continue
    return classes


def _collect_dataset_configs_from_models(model_classes: dict) -> dict:
    """
    Call get_train_config() on every model and collect unique datasets.

    For each dataset, accumulates the union of raw columns required by all
    models that use it. This ensures the fixture file has every column any
    model might need.

    Returns a dict of {dataset_class_name: (dataset_cls, set[str])} where
    the set contains all required raw column names.
    """
    seen: dict = {}  # {name: (dataset_cls, set_of_columns)}

    for model_name, model_cls in model_classes.items():
        try:
            config = model_cls.get_train_config()
        except Exception as e:
            print(f"  WARN  {model_name}.get_train_config() failed: {e}")
            continue

        for entry_name, (_, dataset_cls, (ds_params, _dl)) in config.items():
            key = dataset_cls.__name__

            # Prefer explicit columns list; fall back to input + output features
            if getattr(ds_params, "columns", None):
                cols = set(ds_params.columns)
            else:
                cols = set(getattr(ds_params, "input_features", []) +
                           getattr(ds_params, "output_features", []))

            if key not in seen:
                seen[key] = (dataset_cls, cols)
                print(f"  found {key} (via {model_name}/{entry_name})")
            else:
                # Accumulate columns from additional models using the same dataset
                seen[key][1].update(cols)

    return seen


def _collect_dataset_configs_directly() -> dict:
    """
    Discover datasets directly by scanning DATASET_CLASSES for any class that
    has a ZENODO_URL or ZENODO_BASE_URL Pydantic field.

    This covers datasets that exist without a model pointing to them.
    Returns a dict of {dataset_class_name: (dataset_cls, None)}.
    """
    try:
        from ci.utils import retrieve_instances_from_file
        from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset
    except ImportError as e:
        print(f"Cannot import project packages: {e}", file=sys.stderr)
        return {}

    _BASE_CLASSES = {"SeanergysDataset", "SeanergysParquetDataset"}
    seen = {}

    for py_file in (PROJECT_ROOT / "seanergys_modelzoo" / "datasets").rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            classes = retrieve_instances_from_file(py_file, SeanergysDataset)
        except Exception:
            continue
        for name, cls in classes.items():
            if cls.__name__ in _BASE_CLASSES:
                continue
            fields = cls.model_fields
            if "ZENODO_URL" in fields or "ZENODO_BASE_URL" in fields:
                if name not in seen:
                    seen[name] = (cls, None)
                    print(f"  found {cls.__name__} (via direct dataset discovery)")

    return seen


def _collect_dataset_configs(model_classes: dict) -> dict:
    """
    Collect unique datasets from both model get_train_config() and direct
    dataset discovery (for datasets with ZENODO_URL/ZENODO_BASE_URL fields).

    Model-based discovery takes priority (deduplicated by dataset class name).
    """
    seen = _collect_dataset_configs_from_models(model_classes)

    for key, value in _collect_dataset_configs_directly().items():
        if key not in seen:
            seen[key] = value

    return seen


def create_sample(dataset_cls, required_cols, max_rows: int, out_dir: Path) -> Path:
    """
    Download a small raw sample from Zenodo and save to parquet.

    WHY WE BYPASS THE DATASET CLASS ENTIRELY
    -----------------------------------------
    Going through the dataset class has several problems:

    1. is_dummy=True in FDataDataset only adds date filters — it still hits Zenodo.
    2. Column filtering: MACK uses output_features=["mem_compute_bound"] which
       only exists in some files, not the latest one.
    3. Transforms require fitted models (embedding_parsing, etc.) which cannot
       run at sample creation time — the model has not been trained yet.

    SOLUTION: download raw parquet directly with pandas (all columns, no filters).
    For multi-file datasets, try files from newest to oldest until a file that
    contains all required columns is found. Take head(max_rows) and save all
    columns — the integration tests select only the columns they need.

    Args:
        required_cols: set of column names that must be present in the fixture.
                       None means any file is acceptable.
    """
    import pandas as pd

    # ZENODO_BASE_URL and ZENODO_URL are Pydantic fields (not plain class attrs),
    # so we read their default values from model_fields rather than the class directly.
    fields = dataset_cls.model_fields

    # --- Multi-file datasets (e.g. FDataDataset: 38 monthly parquet files) ---
    if "ZENODO_BASE_URL" in fields and "AVAILABLE_FILES" in fields:
        base_url = fields["ZENODO_BASE_URL"].default
        available_files = fields["AVAILABLE_FILES"].default

        # Try files from newest to oldest until one contains all required columns.
        df = None
        for file_name in reversed(available_files):
            url = f"{base_url}/{file_name}.parquet"
            print(f"         trying {file_name}.parquet ...")
            candidate = pd.read_parquet(url)
            if required_cols and not required_cols.issubset(set(candidate.columns)):
                missing = required_cols - set(candidate.columns)
                print(f"           missing columns {missing}, trying older file...")
                continue
            df = candidate
            print(f"           OK — has all required columns.")
            break

        if df is None:
            raise ValueError(
                f"{dataset_cls.__name__}: no file in AVAILABLE_FILES contains all "
                f"required columns: {required_cols}"
            )

    # --- Single-file datasets (e.g. PM100Dataset: one parquet URL) ---
    elif "ZENODO_URL" in fields:
        url = fields["ZENODO_URL"].default
        print(f"         downloading from Zenodo (no column filter)...")
        df = pd.read_parquet(url)

    else:
        raise ValueError(
            f"{dataset_cls.__name__} has no ZENODO_BASE_URL or ZENODO_URL Pydantic field. "
            "Cannot determine download URL. Add one of these fields to the dataset class."
        )

    sample = df.head(max_rows)

    # Consistent naming: FDataDataset → sample_fdata.parquet
    name = dataset_cls.__name__.lower().replace("dataset", "")
    out_path = out_dir / f"sample_{name}.parquet"
    sample.to_parquet(out_path, index=False)

    return out_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Create sample fixture parquet files using model get_train_config()."
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Max rows per sample (default: SAMPLE_MAX_ROWS or CI_MAX_SAMPLES or 100).",
    )
    args = parser.parse_args()

    max_rows = args.max_rows or _max_rows()
    out_dir = _output_dir()

    print("Discovering models...")
    model_classes = _discover_model_classes()
    if not model_classes:
        print("No model classes found.")
        sys.exit(1)
    print(f"Found {len(model_classes)} model(s): {', '.join(model_classes)}\n")

    print("Collecting datasets from get_train_config()...")
    dataset_configs = _collect_dataset_configs(model_classes)
    if not dataset_configs:
        print("No datasets found via get_train_config().")
        sys.exit(1)
    print(f"\nWill create samples for: {', '.join(dataset_configs)}")
    print(f"Output dir : {out_dir}")
    print(f"Max rows   : {max_rows}\n")

    errors = []
    for dataset_name, (dataset_cls, required_cols) in dataset_configs.items():
        try:
            print(f"  ...   {dataset_name}")
            out_path = create_sample(dataset_cls, required_cols, max_rows=max_rows, out_dir=out_dir)
            print(f"  OK    {dataset_name} → {out_path.name}")
        except Exception as e:
            print(f"  ERR   {dataset_name}: {e}")
            errors.append(dataset_name)

    print()
    if errors:
        print(
            f"WARNING: {len(errors)} dataset(s) could not be sampled and were skipped: "
            f"{', '.join(errors)}\n"
            "Other fixtures were created successfully. Fix the errors above and re-run "
            "`make sample-data` to fill in the missing ones.",
            file=sys.stderr,
        )
    succeeded = len(dataset_configs) - len(errors)
    print(f"Done. {succeeded}/{len(dataset_configs)} fixture(s) created.")


if __name__ == "__main__":
    main()
