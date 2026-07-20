"""
pipeline_generator — Auto-pipeline generator for ExaMLOps.

At startup, auto-discovers all (model, config) pairs from the active use-case pack (ADR 0094;
default ``usecases/seanergy``, override with ``EXAMLOPS_USECASE_DIR``) by scanning:
  • the pack's model ``tasks_root`` package  → framework model subclasses
  • the pack's ``config_package``            → framework config subclasses

Each config class must declare MODEL_CLASS = <ModelClass> to be matched.
Matched pairs are registered automatically — no manual wiring needed.

Then runs a generic Prefect training flow for every (model × dataset):

    data_extraction → slurm_submit → slurm_wait → result_fetch
        → evaluate → MLflow log → promote to Production

HPC mode is controlled by EXAMLOPS_SLURM_MODE (default: mock).
  mock  — training runs inline in the Prefect worker process
  slurm — training is submitted as an sbatch job on a real HPC cluster

Usage:
    # Run all discovered models:
    python pipelines/pipeline_generator.py

    # Specific model + dataset:
    python pipelines/pipeline_generator.py --model JPCP --dataset PM100Dataset

    # Dry run with dummy data (no Zenodo download):
    python pipelines/pipeline_generator.py --dummy

    # List all auto-discovered models:
    python pipelines/pipeline_generator.py --list
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import joblib
import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from prefect import flow, task
from prefect.cache_policies import NO_CACHE

# ── Fault-tolerance defaults for pipeline tasks (env-overridable) ────────────────
# Network/IO tasks (dataset download, MLflow logging, result fetch, promotion) get
# retries with exponential backoff and a wall-clock timeout so a transient
# Zenodo/MinIO/MLflow blip or a hung call can't fail or freeze the whole flow.
_IO_RETRIES = int(os.getenv("EXAMLOPS_TASK_IO_RETRIES", "3"))
_IO_RETRY_DELAYS = [5.0, 15.0, 30.0]  # per-attempt backoff seconds
_DATA_TIMEOUT_S = int(os.getenv("EXAMLOPS_TASK_DATA_TIMEOUT_S", "1800"))
_MLFLOW_TIMEOUT_S = int(os.getenv("EXAMLOPS_TASK_MLFLOW_TIMEOUT_S", "600"))
_FETCH_TIMEOUT_S = int(os.getenv("EXAMLOPS_TASK_FETCH_TIMEOUT_S", "300"))
_SUBMIT_TIMEOUT_S = int(os.getenv("EXAMLOPS_TASK_SUBMIT_TIMEOUT_S", "300"))

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODELZOO = _REPO_ROOT / "modelzoo"
_PLATFORM = _REPO_ROOT / "platform"
_SLURM_ADAPTER_DIR = _PLATFORM / "infra" / "slurm-adapter"
for _p in (str(_REPO_ROOT), str(_PLATFORM), str(_MODELZOO), str(_SLURM_ADAPTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ci.utils import retrieve_instances_from_file

from pipelines import usecase as _usecase  # noqa: E402
from pipelines.model_loader import ModelYAMLConfig, scan_model_yamls  # noqa: E402

# registry_loader has no imports from this module — no circular import risk.
from pipelines.registry_loader import export_registry, load_registry, resolve_entries  # noqa: E402

# ── Use-case framework bindings (ADR 0094) ──────────────────────────────────────
# The active pack (default usecases/seanergy; override with EXAMLOPS_USECASE_DIR) supplies the
# ML-framework base classes + helpers. The engine resolves them through the loader so it imports
# nothing use-case-specific by name; a different pack swaps the whole binding.
_FRAMEWORK = _usecase.framework()
DataplaneModel = _FRAMEWORK["model_base"]
DataplaneModelConfiguration = _FRAMEWORK["config_base"]
pipeline_step = _FRAMEWORK["pipeline_step"]  # re-exported for callers  # noqa: F401
_ModelParams = _FRAMEWORK["model_params"]
_Dataloader = _FRAMEWORK["dataloader"]
_DataloaderParams = _FRAMEWORK["dataloader_params"]
_get_backend = _FRAMEWORK["get_backend"]
_adapter_for = _FRAMEWORK["framework_adapter"]
DataplaneModelTask = _FRAMEWORK["model_task"]

# ── Model Registry ─────────────────────────────────────────────────────────────
#
# model_name → (model_cls, config_cls, tasks_dict)
#
# Populated automatically by auto_register_all() at module load time.
# Manual register_model() calls are still supported for overrides.

MODEL_REGISTRY: dict[
    str,
    # middle slot holds either a config *class* or a YAMLBackedConfig *instance*
    tuple[type[DataplaneModel], Any, dict[str, Any]],
] = {}


# ── Auto-discovery ─────────────────────────────────────────────────────────────


def discover_models() -> dict[str, type[DataplaneModel]]:
    """
    Scan the active pack's model ``tasks_root`` package (ADR 0094) and return every
    concrete model subclass found, keyed by class name.
    """
    tasks_root = _usecase.tasks_dir()
    found: dict[str, type[DataplaneModel]] = {}
    for py_file in sorted(tasks_root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            classes = retrieve_instances_from_file(py_file, DataplaneModel)
            found.update(classes)
        except Exception as exc:
            print(f"[discovery] Warning: could not scan {py_file.name}: {exc}")
    return found


def discover_configs() -> dict[str, type[DataplaneModelConfiguration]]:
    """
    Scan the pack's model_configs/*.py and return every concrete
    DataplaneModelConfiguration subclass found, keyed by class name.
    """
    configs_dir = _usecase.config_dir()
    found: dict[str, type[DataplaneModelConfiguration]] = {}
    for py_file in sorted(configs_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            classes = retrieve_instances_from_file(py_file, DataplaneModelConfiguration)
            found.update(classes)
        except Exception as exc:
            print(f"[discovery] Warning: could not scan {py_file.name}: {exc}")
    return found


def auto_register_all() -> None:
    """Scan the pack's models/*.yaml and register each enabled model.

    Replaces the old Python class-scanning auto-discovery. Each YAML file
    provides the full declarative config; the Python shim (config_class field)
    supplies model-bound transforms.

    A model already in MODEL_REGISTRY is not re-registered (manual calls win).
    """
    for yaml_cfg in scan_model_yamls(_MODELS_DIR):
        if not yaml_cfg.enabled:
            continue
        if yaml_cfg.name in MODEL_REGISTRY:
            continue
        register_model_from_yaml(yaml_cfg)


# ── Registration ───────────────────────────────────────────────────────────────


def register_model(
    model_cls: type[DataplaneModel],
    config_cls: type[DataplaneModelConfiguration],
) -> None:
    """
    Register a model + configurator pair.

    Builds Prefect tasks at registration time from every @pipeline_step method
    on the model class (train_step, evaluate_step, and any custom steps).
    Stores (model_cls, config_cls, tasks) in MODEL_REGISTRY.
    """
    tasks = _build_model_tasks(model_cls, config_cls)
    MODEL_REGISTRY[model_cls.__name__] = (model_cls, config_cls, tasks)


# ── Pipeline step task builder ─────────────────────────────────────────────────


def _build_model_tasks(
    model_cls: type[DataplaneModel],
    config_cls: Any,  # config class or YAMLBackedConfig instance
) -> dict[str, Any]:
    """
    Scan model_cls for @pipeline_step methods and return a dict of
    Prefect Task callables keyed by method name.

    Kept for custom flows and make_prefect_tasks_from_model().
    The standard training_flow uses the shared HPC tasks below instead.
    """
    step_attrs: dict[str, Any] = {
        attr: getattr(model_cls, attr)
        for attr in dir(model_cls)
        if callable(getattr(model_cls, attr, None))
        and getattr(getattr(model_cls, attr, None), "_is_pipeline_step", False)
    }

    tasks: dict[str, Any] = {}

    if "train_step" in step_attrs:
        retries = step_attrs["train_step"]._step_retries

        def _make_train(cfg_cls: Any, rt: int) -> Any:
            @task(name=f"{model_cls.__name__}_train", retries=rt)
            def _train(
                model_name: str,
                dataset_cls_name: str,
                is_dummy: bool = False,
            ) -> tuple[Any, dict]:
                ds_cls = _resolve_dataset_cls(cfg_cls, dataset_cls_name)
                model, _, loader = cfg_cls.get_train_components(
                    ds_cls, split="train", is_dummy=is_dummy, backend_name=None
                )
                print(f"[pipeline] Training {model_name} on {dataset_cls_name}...")
                history = model.train_step(loader)
                print(f"[pipeline] Training done — keys: {list(history.keys())}")
                return model, history

            return _train

        tasks["train_step"] = _make_train(config_cls, retries)

    if "evaluate_step" in step_attrs:

        def _make_evaluate(cfg_cls: Any) -> Any:
            @task(name=f"{model_cls.__name__}_evaluate", cache_policy=NO_CACHE)
            def _evaluate(
                model: Any,
                model_name: str,
                dataset_cls_name: str,
                is_dummy: bool = False,
            ) -> dict:
                from sklearn.metrics import mean_absolute_percentage_error, mean_squared_error

                ds_cls = _resolve_dataset_cls(cfg_cls, dataset_cls_name)
                _, _, val_loader = cfg_cls.get_train_components(
                    ds_cls, split="validation", is_dummy=is_dummy, backend_name=None
                )
                X_val, y_val = model._extract_data_from_loader(val_loader)
                y_pred = model.estimator.predict(X_val)

                rmse = float(np.sqrt(mean_squared_error(y_val, y_pred)))
                mape = float(mean_absolute_percentage_error(y_val, y_pred) * 100)
                mse = float(mean_squared_error(y_val, y_pred))
                metrics = {"rmse": rmse, "mape": mape, "mse": mse}
                print(f"[pipeline] Eval — RMSE: {rmse:.4f}  MAPE: {mape:.2f}%  MSE: {mse:.4f}")
                return metrics

            return _evaluate

        tasks["evaluate_step"] = _make_evaluate(config_cls)

    return tasks


def make_prefect_tasks_from_model(model_cls: type[DataplaneModel]) -> dict[str, Any]:
    """
    Public utility: scan a model class for @pipeline_step methods and return
    a dict mapping step attribute name → Prefect Task callable.

    Unlike _build_model_tasks, tasks returned here accept
    (model_instance, *args, **kwargs) directly — useful for custom flows.
    """
    discovered: dict[str, Any] = {}
    for attr_name in dir(model_cls):
        method = getattr(model_cls, attr_name, None)
        if not (callable(method) and getattr(method, "_is_pipeline_step", False)):
            continue

        step_name = method._step_name
        step_retries = method._step_retries

        def _make_task(mname: str, sname: str, sr: int) -> Any:
            @task(name=sname, retries=sr)
            def _prefect_task(model_instance: Any, *args: Any, **kwargs: Any) -> Any:
                return getattr(model_instance, mname)(*args, **kwargs)

            return _prefect_task

        discovered[attr_name] = _make_task(attr_name, step_name, step_retries)

    return discovered


# ── YAML-backed config helpers ────────────────────────────────────────────────

_MODELS_DIR = _usecase.models_dir()

_DATASET_CLASS_MAP: dict[str, type] | None = None


def _get_dataset_class_map() -> dict[str, type]:
    global _DATASET_CLASS_MAP
    if _DATASET_CLASS_MAP is None:
        _DATASET_CLASS_MAP = _usecase.dataset_registry()
    return _DATASET_CLASS_MAP


def _resolve_dataset_cls_by_name(name: str) -> type:
    m = _get_dataset_class_map()
    if name not in m:
        raise ValueError(f"Unknown dataset {name!r}. Known: {sorted(m)}")
    return m[name]


def _import_shim(config_class: str):
    """Dynamically import a Python shim class from 'module.ClassName' notation.

    E.g. 'jpcp_config.JPCPConfiguration' → pipelines.model_configs.jpcp_config.JPCPConfiguration
    """
    module_name, class_name = config_class.rsplit(".", 1)
    module = importlib.import_module(f"{_usecase.config_package()}.{module_name}")
    return getattr(module, class_name)


def _parse_filter_value(v: str):
    """Convert ISO 8601 datetime strings to pd.Timestamp; plain date strings (no T) stay as str.

    PM100's timestamp columns expect pd.Timestamp values; FData's date columns (adt etc.)
    are stored as strings in the parquet files and require string comparison.
    Dates with a time component ("2020-05-01T00:00:00+00:00") are full timestamps;
    plain dates ("2023-12-01") are string-typed in FData parquet files.
    """
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}T", v):
        return pd.Timestamp(v)
    return v


def _build_train_components(
    yaml_cfg: ModelYAMLConfig,
    shim: Any,
    dataset_cls: type,
    split: str,
    is_dummy: bool,
    backend_name: str | None,
) -> tuple:
    """Build (model, dataset, loader) from YAML config + Python shim transforms."""
    # Framework helpers come from the active pack (ADR 0094) — bound at module load.
    ds_name = dataset_cls.__name__
    ds_entry = yaml_cfg.dataset(ds_name)
    split_cfg = yaml_cfg.split_config(ds_name, split)

    # Build DataplaneModelParams from YAML model section
    model_dict = dict(yaml_cfg.model)
    raw_embedding = model_dict.pop("embedding_type", None)
    hyperparameters = model_dict.pop("hyperparameters", {})
    if raw_embedding is not None:
        model_dict["embedding_type"] = shim.resolve_embedding_type(raw_embedding)
    model_dict["model_hyperparameters"] = hyperparameters
    model_params = _ModelParams(**model_dict)

    model = shim.MODEL_CLASS(**model_params.to_dict())

    # Get model-bound transforms from Python shim
    transforms = shim.get_transforms(model, dataset_cls)

    # Build dataset kwargs
    filters = [(f[0], f[1], _parse_filter_value(f[2])) for f in split_cfg.filters]
    ds_kwargs: dict = {
        "is_dummy": is_dummy,
        "input_features": ds_entry.input_features,
        "output_features": ds_entry.output_features,
        "filters": filters,
    }
    if ds_entry.cache_dir:
        ds_kwargs["download_path"] = str(_REPO_ROOT / ds_entry.cache_dir)
    if ds_entry.columns:
        ds_kwargs["columns"] = ds_entry.columns
    if split_cfg.files:
        ds_kwargs["files"] = split_cfg.files

    # Merge transforms (target_transform, transform, preprocessing_functions)
    ds_kwargs.update(transforms)

    # Split-aware datasets use the split name to
    # draw disjoint train/validation/test samples. Datasets that don't model a
    # split accept it as an ignored extra field (extra="allow").
    ds_kwargs["split"] = split

    # Backend: YAML default overridden by runtime arg
    effective_backend = backend_name or ds_entry.backend
    if effective_backend and effective_backend != "zenodo":
        ds_kwargs["backend"] = _get_backend(effective_backend)
    else:
        ds_kwargs["use_zenodo_url"] = True

    loader_params = _DataloaderParams(batch_size=ds_entry.batch_size)
    dataset = dataset_cls(**ds_kwargs)
    loader = _Dataloader(dataset, **loader_params.to_dict())
    return model, dataset, loader


class YAMLBackedConfig:
    """Drop-in for DataplaneModelConfiguration, backed by a per-model YAML file.

    Stored in MODEL_REGISTRY in the config_cls slot. Implements the same
    interface as DataplaneModelConfiguration so all existing pipeline tasks
    work without changes.
    """

    def __init__(self, yaml_cfg: ModelYAMLConfig, shim: Any) -> None:
        self._yaml = yaml_cfg
        self._shim = shim
        self.__name__ = f"YAMLBackedConfig[{yaml_cfg.name}]"
        self.MODEL_CLASS = shim.MODEL_CLASS
        self.SUPPORTED_DATASETS = [
            _resolve_dataset_cls_by_name(ds.name) for ds in yaml_cfg.datasets
        ]

    def get_inference_params(self) -> dict:
        y = self._yaml
        prod = next((r for r in y.lifecycle if r["name"] == "Production"), {})
        return {
            "model_id": y.serving.get("model_id", y.name.lower()),
            "lifecycle": y.lifecycle,
            "promotion_metric": prod.get("metric", ""),
            "promotion_threshold": float(prod.get("threshold", 0.0)),
            "promotion_direction": prod.get("direction", "lower_is_better"),
            "input_schema": y.inference.get("input_schema", {}),
            "output_schema": y.inference.get("output_schema", {}),
        }

    def get_train_components(
        self,
        dataset_cls: type,
        split: str = "train",
        is_dummy: bool = False,
        backend_name: str | None = None,
    ) -> tuple:
        return _build_train_components(
            self._yaml, self._shim, dataset_cls, split, is_dummy, backend_name
        )


def register_model_from_yaml(yaml_cfg: ModelYAMLConfig) -> None:
    """Register a model from a per-model YAML config + Python shim.

    Creates a YAMLBackedConfig as the config_cls slot in MODEL_REGISTRY so all
    existing pipeline tasks work without changes.
    """
    shim = _import_shim(yaml_cfg.config_class)
    backed_cfg = YAMLBackedConfig(yaml_cfg, shim)
    tasks = _build_model_tasks(shim.MODEL_CLASS, backed_cfg)
    MODEL_REGISTRY[yaml_cfg.name] = (shim.MODEL_CLASS, backed_cfg, tasks)
    print(
        f"[discovery] Registered {yaml_cfg.name} ← {yaml_cfg.config_class}"
        f"  datasets={[d.name for d in yaml_cfg.datasets]}"
    )


# ── Auto-register at import time ───────────────────────────────────────────────

auto_register_all()

# Manual overrides — uncomment to force a specific pairing regardless of
# MODEL_CLASS on the config:
#   register_model(MACK, MACKConfiguration)


# ── YAML registry overlay ─────────────────────────────────────────────────────


def _build_dataset_lookup() -> dict[str, type]:
    """Map dataset class name → class object from all registered SUPPORTED_DATASETS.

    Uses the class-level __name__ attribute when present (supports test fakes
    that set __name__ as a class attribute), falling back to the real Python
    class name via type.__name__.
    """
    lookup: dict[str, type] = {}
    for _, (_, cfg, _) in MODEL_REGISTRY.items():
        for ds in cfg.SUPPORTED_DATASETS:
            # vars(ds).get("__name__") picks up class-attribute overrides
            # (used by test fakes); ds.__name__ is the real Python type name.
            name = vars(ds).get("__name__") or ds.__name__
            lookup[name] = ds
    return lookup


def _make_yaml_override_config(
    base_config_cls: Any,  # a DataplaneModelConfiguration subclass (classmethods called below)
    entry: Any,
    dataset_classes: list[type],
) -> type:
    """Return a dynamic subclass of base_config_cls with YAML-driven overrides.

    Preserves all Python logic (transforms, embeddings, hyperparameters).
    Overrides only: SUPPORTED_DATASETS, lifecycle, backend, dummy.
    """
    _lifecycle = entry.lifecycle
    _backend = entry.backend
    _dummy = entry.dummy
    _datasets = dataset_classes

    class _YamlOverrideConfig(base_config_cls):  # type: ignore[valid-type]
        SUPPORTED_DATASETS = _datasets

        @classmethod
        def get_inference_params(cls) -> dict:
            params = base_config_cls.get_inference_params()
            if _lifecycle:
                params = {**params, "lifecycle": _lifecycle}
            return params

        @classmethod
        def get_train_components(
            cls,
            dataset_cls: type,
            split: str = "train",
            is_dummy: bool = False,
            backend_name: str | None = None,
        ) -> tuple:
            return base_config_cls.get_train_components(
                dataset_cls,
                split=split,
                is_dummy=is_dummy or _dummy,
                backend_name=backend_name if backend_name is not None else _backend,
            )

    _YamlOverrideConfig.__name__ = f"{base_config_cls.__name__}_yaml_{entry.name}"
    _YamlOverrideConfig.__qualname__ = _YamlOverrideConfig.__name__
    return _YamlOverrideConfig


def apply_yaml_registry(
    registry_path: Path | str | None = None,
    env_path: Path | str | None = None,
) -> None:
    """Apply a YAML registry overlay on top of MODEL_REGISTRY.

    Safe to call with a nonexistent path — becomes a no-op.
    Auto-detects pipelines/model_registry.yaml when registry_path is None.
    """
    if registry_path is None:
        default = _REPO_ROOT / "pipelines" / "model_registry.yaml"
        if not default.exists():
            return
        registry_path = default

    registry_path = Path(registry_path)
    if not registry_path.exists():
        return

    env_path = Path(env_path) if env_path else None
    all_entries = load_registry(registry_path, env_path)

    disabled_names = {e.name for e in all_entries if not e.enabled}
    enabled_entries = [e for e in all_entries if e.enabled]

    for name in disabled_names:
        MODEL_REGISTRY.pop(name, None)

    # Build dataset lookup before resolving (disabled entries already removed)
    ds_lookup = _build_dataset_lookup()

    for entry in enabled_entries:
        try:
            resolved_list = resolve_entries([entry], MODEL_REGISTRY)
        except ValueError as exc:
            print(f"[yaml_registry] WARNING: skipping '{entry.name}' — {exc}")
            continue
        r = resolved_list[0]

        missing = [n for n in entry.datasets if n not in ds_lookup]
        if missing:
            print(
                f"[yaml_registry] WARNING: dataset(s) {missing} for '{entry.name}' "
                f"not found in any config's SUPPORTED_DATASETS — skipping."
            )
            continue

        dataset_classes = [ds_lookup[n] for n in entry.datasets]
        override_cfg = _make_yaml_override_config(r.config_cls, entry, dataset_classes)

        action = "updated" if entry.name in MODEL_REGISTRY else "added"
        existing_tasks = MODEL_REGISTRY.get(entry.name, (None, None, {}))[2]
        MODEL_REGISTRY[entry.name] = (r.model_cls, override_cfg, existing_tasks)
        print(f"[yaml_registry] {action} '{entry.name}' (class={r.model_cls.__name__})")


# ── HPC pipeline tasks ─────────────────────────────────────────────────────────
#
# These seven tasks form the standard training_flow for every model.
# All are generic (model-agnostic) and look up MODEL_REGISTRY internally.


@task(
    name="data_extraction",
    cache_policy=NO_CACHE,
    retries=_IO_RETRIES,
    retry_delay_seconds=_IO_RETRY_DELAYS,
    timeout_seconds=_DATA_TIMEOUT_S,
)
def data_extraction_task(
    model_name: str,
    dataset_cls_name: str,
    is_dummy: bool = False,
    backend_name: str | None = None,
) -> tuple[Any, Any]:
    """
    Instantiate the model (with hyperparams/embeddings) and build the training
    dataloader. No training happens here.

    Returns (model_init, train_loader).
    """
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    ds_cls = _resolve_dataset_cls(config_cls, dataset_cls_name)
    model_init, _, train_loader = config_cls.get_train_components(
        ds_cls, split="train", is_dummy=is_dummy, backend_name=backend_name
    )
    print(
        f"[data_extraction] {model_name} × {dataset_cls_name}  dummy={is_dummy}  backend={backend_name or 'legacy'}"
    )
    return model_init, train_loader


# ── HPC scheduler helpers ───────────────────────────────────────────────────────


def _hpc_scheduler_name() -> str:
    """Resolve the scheduler backend, honoring the legacy EXAMLOPS_SLURM_MODE."""
    sched = os.getenv("EXAMLOPS_HPC_SCHEDULER", "").lower().strip()
    if sched:
        return sched
    return (
        "slurm" if os.getenv("EXAMLOPS_SLURM_MODE", "mock").lower().strip() == "slurm" else "mock"
    )


def _hpc_resources(model_name: str, dataset_cls_name: str) -> dict:
    """Scheduler-neutral resource dict. EXAMLOPS_HPC_* wins; EXAMLOPS_SLURM_* is the fallback."""

    def pick(*names: str, default: str | None = None) -> str | None:
        for name in names:
            val = os.getenv(name)
            if val is not None:
                return val
        return default

    raw = {
        "partition": pick("EXAMLOPS_HPC_PARTITION", "EXAMLOPS_SLURM_PARTITION"),
        "qos": os.getenv("EXAMLOPS_HPC_QOS"),
        "account": os.getenv("EXAMLOPS_HPC_ACCOUNT"),
        "constraint": os.getenv("EXAMLOPS_HPC_CONSTRAINT"),
        "time": pick("EXAMLOPS_HPC_TIME", "EXAMLOPS_SLURM_TIME", default="2:00:00"),
        "nodes": pick("EXAMLOPS_HPC_NODES", "EXAMLOPS_SLURM_NODES", default="1"),
        "ntasks": os.getenv("EXAMLOPS_HPC_NTASKS", "1"),
        "cpus_per_task": pick("EXAMLOPS_HPC_CPUS", "EXAMLOPS_SLURM_CPUS", default="4"),
        "mem": pick("EXAMLOPS_HPC_MEM", "EXAMLOPS_SLURM_MEM", default="16G"),
        "gpus": os.getenv("EXAMLOPS_HPC_GPUS"),
        "job_name": f"examlops_{model_name.lower()}_{dataset_cls_name.lower()}",
    }
    return {k: v for k, v in raw.items() if v is not None}


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _current_flow_run_id() -> str | None:
    try:
        from prefect.runtime import flow_run  # noqa: PLC0415

        return flow_run.get_id()
    except Exception:  # noqa: BLE001 - not always inside a flow-run context
        return None


def _record_hpc_job_safe(
    job_id: str, scheduler: str, model: str, dataset: str, resources: dict
) -> None:
    """Best-effort insert of an hpc_jobs tracking row (never breaks the flow)."""
    try:
        from examlops.platform_db import record_hpc_job  # noqa: PLC0415

        record_hpc_job(
            job_id=job_id,
            scheduler=scheduler,
            flow_run_id=_current_flow_run_id(),
            model=model,
            dataset=dataset,
            nodes=_int_or_none(resources.get("nodes")),
            gpus=_int_or_none(resources.get("gpus")),
            cpus=_int_or_none(resources.get("cpus_per_task")),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[hpc] record_hpc_job skipped: {exc}")


def _update_hpc_job_safe(job_id: str, scheduler: str, status: dict) -> None:
    """Best-effort update of an hpc_jobs row with terminal status."""
    try:
        from examlops.platform_db import update_hpc_job  # noqa: PLC0415

        update_hpc_job(
            job_id=job_id,
            scheduler=scheduler,
            state=status.get("state"),
            start_time=status.get("start_time"),
            end_time=status.get("end_time"),
            exit_code=status.get("exit_code"),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[hpc] update_hpc_job skipped: {exc}")


@task(
    name="slurm_submit",
    retries=2,
    retry_delay_seconds=[10, 30],
    timeout_seconds=_SUBMIT_TIMEOUT_S,
    cache_policy=NO_CACHE,
)
def slurm_submit_task(
    model: Any,
    loader: Any,
    model_name: str,
    dataset_cls_name: str,
    is_dummy: bool = False,
) -> tuple[str, str | None]:
    """
    Submit training to HPC (or run inline for mock mode).

    EXAMLOPS_SLURM_MODE=mock  (default):
        Runs model.train_step(loader) inline, saves estimator to a temp pkl,
        returns (job_id, artifact_path).

    EXAMLOPS_HPC_SCHEDULER=slurm|flux:
        Submits a batch job via the scheduler adapter (over SSH when configured),
        returns (job_id, remote_model_path) — fetched back after the job completes.
    """
    scheduler = _hpc_scheduler_name()

    if scheduler == "mock":
        print(f"[slurm_submit] mock — training {model_name} on {dataset_cls_name} inline")
        model.train_step(loader)

        tmp_dir = Path(tempfile.mkdtemp(prefix="examlops_mock_"))
        artifact_path = tmp_dir / "model.pkl"
        joblib.dump(model.estimator, artifact_path)

        job_id = f"mock-{uuid.uuid4().hex[:8]}"
        print(f"[slurm_submit] job_id={job_id}  artifact={artifact_path}")
        return job_id, str(artifact_path)

    # Real HPC (slurm | flux) — generate a portable bash wrapper and submit via the
    # scheduler adapter. All resource directives flow through CLI flags (not #SBATCH
    # comments), so the same script works for sbatch and flux batch.
    from adapter import get_scheduler_adapter  # noqa: PLC0415

    adapter = get_scheduler_adapter()

    run_uuid = uuid.uuid4().hex[:12]
    remote_base = os.getenv("EXAMLOPS_HPC_REMOTE_WORKDIR", str(adapter.working_dir))
    remote_dir = f"{remote_base}/{run_uuid}"
    remote_model = f"{remote_dir}/model.pkl"

    mlflow_uri = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000")
    remote_repo = os.getenv("EXAMLOPS_HPC_REMOTE_REPO", str(_REPO_ROOT))
    remote_python = os.getenv(
        "EXAMLOPS_HPC_REMOTE_PYTHON", str(Path(remote_repo) / ".venv" / "bin" / "python")
    )
    train_script = f"{remote_repo}/pipelines/slurm_train_script.py"

    local_job_dir = Path(adapter.working_dir) / run_uuid
    local_job_dir.mkdir(parents=True, exist_ok=True)
    bash_script = local_job_dir / "run.sh"
    # Forward --dummy so a real-scheduler smoke test trains on the small dummy split
    # instead of the full dataset (parity with mock mode and the CLI --dummy flag).
    dummy_flag = " --dummy" if is_dummy else ""
    bash_script.write_text(
        "#!/bin/bash\n"
        f"mkdir -p {remote_dir}\n"
        f"{remote_python} {train_script} \\\n"
        f"  --model {model_name} \\\n"
        f"  --dataset {dataset_cls_name} \\\n"
        f"  --output {remote_model} \\\n"
        f"  --mlflow-uri {mlflow_uri}{dummy_flag}\n"
    )
    bash_script.chmod(0o755)

    resources = _hpc_resources(model_name, dataset_cls_name)
    job_id = adapter.submit_job(
        script_path=str(bash_script), resources=resources, remote_dir=remote_dir
    )
    _record_hpc_job_safe(job_id, scheduler, model_name, dataset_cls_name, resources)
    print(f"[slurm_submit] {scheduler} job_id={job_id}  remote_dir={remote_dir}")
    return job_id, remote_model


@task(name="slurm_wait", cache_policy=NO_CACHE)
def slurm_wait_task(
    job_id: str,
    artifact_path: str | None,
) -> tuple[str, str]:
    """
    Wait for the HPC job to reach a terminal state.

    Mock mode: artifact_path is a local pkl — returns immediately.
    Real HPC: polls the scheduler until terminal, then fetches the remote model back.
    """
    scheduler = _hpc_scheduler_name()

    if scheduler == "mock":
        # Mock: training already finished in slurm_submit_task
        print(f"[slurm_wait] {job_id} → COMPLETED (mock)")
        return "COMPLETED", artifact_path or ""

    # Real HPC (slurm | flux)
    from adapter import get_scheduler_adapter  # noqa: PLC0415

    adapter = get_scheduler_adapter()
    adapter.wait_until_complete(job_id)
    status = adapter.get_job_status(job_id)
    state = status.get("state", "UNKNOWN")
    _update_hpc_job_safe(job_id, scheduler, status)
    print(f"[slurm_wait] {job_id} → {state}")

    if state != "COMPLETED":
        raise RuntimeError(f"HPC job {job_id} ended with state '{state}'. Check scheduler logs.")

    # Fetch the trained estimator back from the (possibly remote) cluster. For the
    # local/shared-FS case executor.get is a plain copy, so this is a no-op move.
    remote_model = artifact_path or f"{adapter.remote_jobdir(job_id)}/model.pkl"
    local_dir = Path(tempfile.mkdtemp(prefix="examlops_hpc_"))
    local_model = local_dir / "model.pkl"
    adapter.executor.get(remote_model, str(local_model))
    return state, str(local_model)


@task(
    name="result_fetch",
    cache_policy=NO_CACHE,
    retries=2,
    retry_delay_seconds=[5, 15],
    timeout_seconds=_FETCH_TIMEOUT_S,
)
def result_fetch_task(
    state: str,
    artifact_path: str,
    model_name: str,
    dataset_cls_name: str,
    is_dummy: bool = False,
    backend_name: str | None = None,
) -> Any:
    """
    Load the trained estimator from disk and inject it into a fresh model
    instance that carries the correct hyperparams, metadata, and embeddings.

    Returns a fully-populated DataplaneModel ready for evaluate_task.
    """
    if state != "COMPLETED":
        raise RuntimeError(f"Cannot fetch result: job state is '{state}'")

    _, config_cls, _ = MODEL_REGISTRY[model_name]
    ds_cls = _resolve_dataset_cls(config_cls, dataset_cls_name)
    model_init, _, _ = config_cls.get_train_components(
        ds_cls, split="train", is_dummy=is_dummy, backend_name=backend_name
    )

    # Phase 5: dispatch on framework. Sklearn models keep using joblib; PyTorch
    # / HuggingFace models load via their adapter so the same code path serves
    # every framework. The adapter comes from the active pack (ADR 0094).
    adapter = _adapter_for(model_init)
    loaded_estimator = adapter.load(model_init, Path(artifact_path))
    print(
        f"[result_fetch] Loaded {adapter.flavour} estimator from {artifact_path} "
        f"→ {type(loaded_estimator).__name__}"
    )
    return model_init


@task(
    name="evaluate",
    cache_policy=NO_CACHE,
    retries=2,
    retry_delay_seconds=[10, 30],
    timeout_seconds=_DATA_TIMEOUT_S,
)
def evaluate_task(
    model: Any,
    model_name: str,
    dataset_cls_name: str,
    is_dummy: bool = False,
    backend_name: str | None = None,
) -> dict:
    """Evaluate the trained model on the validation split. Returns metrics dict."""
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    ds_cls = _resolve_dataset_cls(config_cls, dataset_cls_name)
    _, _, val_loader = config_cls.get_train_components(
        ds_cls, split="validation", is_dummy=is_dummy, backend_name=backend_name
    )
    X_val, y_val = model._extract_data_from_loader(val_loader)
    y_pred = model.estimator.predict(X_val)

    is_classification = (
        hasattr(model, "task_type") and model.task_type == DataplaneModelTask.CLASSIFICATION
    )

    if is_classification:
        from sklearn.metrics import accuracy_score, f1_score

        accuracy = float(accuracy_score(y_val, y_pred))
        f1 = float(f1_score(y_val, y_pred, average="weighted", zero_division=0))
        metrics = {"accuracy": accuracy, "f1": f1}
        print(f"[evaluate] Accuracy: {accuracy:.4f}  F1: {f1:.4f}")
    else:
        from sklearn.metrics import mean_absolute_percentage_error, mean_squared_error

        rmse = float(np.sqrt(mean_squared_error(y_val, y_pred)))
        mape = float(mean_absolute_percentage_error(y_val, y_pred) * 100)
        mse = float(mean_squared_error(y_val, y_pred))
        metrics = {"rmse": rmse, "mape": mape, "mse": mse}
        print(f"[evaluate] RMSE: {rmse:.4f}  MAPE: {mape:.2f}%  MSE: {mse:.4f}")

    return metrics


# ── Infrastructure tasks (model-agnostic) ──────────────────────────────────────


def _pin_dataset_revision(dataset_name: str, backend_name: str | None, run_id: str) -> None:
    """A1 (ADR 0003): resolve, tag, and record the dataset revision for this run.

    Fully fail-open (spec R4/R13): any error is swallowed so a revision hiccup can
    never fail a training run. Honours an explicit ``EXAMLOPS_DATASET_REVISION`` pin
    (spec R12) and otherwise records the resolved revision (lakeFS commit or
    ``unknown`` when content can't be materialised in this context).
    """
    try:
        from examlops.platform_db import record_dataset_revision  # noqa: PLC0415
        from pipelines.datasets.versioning import resolve_revision  # noqa: PLC0415

        rev = resolve_revision(backend_name, dataset_name)
        pinned = os.getenv("EXAMLOPS_DATASET_REVISION", "").strip()
        revision_id = pinned or rev.revision_id
        backend = rev.backend
        uri = rev.uri
        mlflow.set_tag("dataset_revision", revision_id)
        mlflow.set_tag("dataset_backend", backend)
        mlflow.set_tag("dataset_uri", uri)
        # Reconstruct a lightweight rev carrying the effective id for recording.
        from dataclasses import replace  # noqa: PLC0415

        eff = replace(rev, revision_id=revision_id)
        record_dataset_revision(
            eff,
            mlflow_run_id=run_id or None,
            row_count=rev.row_count,
            byte_count=rev.byte_count,
            actor=os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "pipeline",
        )
        print(f"[pipeline] dataset revision pinned: {backend}@{revision_id} ({rev.kind})")
    except Exception as exc:  # noqa: BLE001 - fail-open, never break a run
        print(f"[pipeline] dataset revision pin skipped: {exc}")


@task(
    name="log_mlflow",
    cache_policy=NO_CACHE,
    retries=_IO_RETRIES,
    retry_delay_seconds=_IO_RETRY_DELAYS,
    timeout_seconds=_MLFLOW_TIMEOUT_S,
)
def log_mlflow_task(
    model: Any,
    metrics: dict,
    model_name: str,
    dataset_name: str,
    job_id: str | None = None,
    scheduler: str | None = None,
    backend_name: str | None = None,
) -> dict:
    """Log model + metrics to MLflow. Returns registration dict."""
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    inference_params = config_cls.get_inference_params()
    registered_model_name = inference_params["model_id"]

    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:15000"))
    # Project Anatomy P6 (ADR 0091): when the model belongs to a project, route its artifacts into
    # the project's own storage prefix via a per-project MLflow experiment. Fail-open — any error
    # (no project, MLflow unavailable) falls back to the default per-model experiment, so a
    # non-project run is completely unaffected.
    _experiment = f"{model_name.lower()}_{dataset_name.lower()}"
    try:
        from examlops.platform_db import (  # noqa: PLC0415
            ensure_project_storage,
            project_experiment,
        )
        from examlops.project_scope import resolve_project  # noqa: PLC0415

        _project = resolve_project(model_name)
        if _project:
            _store = ensure_project_storage(_project)
            _experiment = project_experiment(_project)
            _artifact_loc = (
                f"s3://{_store['bucket']}/{_store['prefix']}artifacts" if _store else None
            )
            if mlflow.get_experiment_by_name(_experiment) is None and _artifact_loc:
                mlflow.create_experiment(_experiment, artifact_location=_artifact_loc)
    except Exception:
        _experiment = f"{model_name.lower()}_{dataset_name.lower()}"
    mlflow.set_experiment(_experiment)

    registration = {"version": None, "run_id": None, "status": "Staging"}

    # Phase 5: pick the right MLflow flavour based on the model's framework
    # attribute (sklearn / pytorch / huggingface). Legacy sklearn models without
    # ``framework`` set fall through to the SklearnFrameworkAdapter — same
    # behaviour as before. The adapter comes from the active pack (ADR 0094).
    adapter = _adapter_for(model)

    with mlflow.start_run(run_name=f"{model_name}_{dataset_name}"):
        mlflow.log_param("model_name", model_name)
        mlflow.log_param("dataset", dataset_name)
        mlflow.log_param("estimator", type(model.estimator).__name__)
        mlflow.log_param("framework", adapter.flavour)
        # Tag the HPC job id so `exa models cost --record` can attribute GPU/CPU-hours
        # back to this run. Scheduler-neutral tag + a legacy alias for older readers.
        if job_id:
            mlflow.set_tag("hpc_job_id", job_id)
            mlflow.set_tag("hpc_scheduler", scheduler or "")
            mlflow.set_tag("slurm_job_id", job_id)  # back-compat
        # Source-level model version (the demo "staleness knob"). Logged so
        # `exa models diff <id> <v1> <v2>` and the MLflow UI can show which
        # source revision produced each registered version. Models without a
        # ``model_version`` attribute simply skip this param.
        source_version = getattr(model, "model_version", None)
        if source_version:
            mlflow.log_param("model_version", str(source_version))
        if model.metadata and model.metadata.hyperparameters:
            mlflow.log_params(model.metadata.hyperparameters)
        mlflow.log_metrics(metrics)

        try:
            adapter.log_mlflow(model, registered_model_name)
            # Tag the model version with the framework so Ray Serve can pick
            # the right flavour at load time. The tag is safe to write even
            # when the version isn't resolved yet — we set it via the
            # MlflowClient after the fact below.
            active_run = mlflow.active_run()
            run_id = active_run.info.run_id if active_run is not None else ""
            registration["run_id"] = run_id
            # A1: pin + record the dataset revision for reproducibility (fail-open).
            _pin_dataset_revision(dataset_name, backend_name, run_id)
            client = mlflow.MlflowClient()
            versions = client.search_model_versions(f"run_id='{run_id}'")
            if versions:
                registration["version"] = str(versions[0].version)
                try:
                    client.set_model_version_tag(
                        registered_model_name,
                        str(versions[0].version),
                        "framework",
                        adapter.flavour,
                    )
                    if source_version:
                        client.set_model_version_tag(
                            registered_model_name,
                            str(versions[0].version),
                            "model_version",
                            str(source_version),
                        )
                except Exception as tag_exc:  # noqa: BLE001
                    print(f"[pipeline] Could not tag framework on version: {tag_exc}")
                print(
                    f"[pipeline] Registered {registered_model_name} v{registration['version']} "
                    f"(framework={adapter.flavour})"
                )
        except Exception as exc:
            print(f"[pipeline] MLflow registration skipped: {exc}")

    return registration


def _evaluate_stage_rule(metric_val: float, threshold: float, direction: str) -> bool:
    """Return True iff *metric_val* satisfies *threshold* under *direction*."""
    if direction == "lower_is_better":
        return metric_val <= threshold
    return metric_val >= threshold


def _notify_ray_serve(model_id: str) -> None:
    """Best-effort webhook to Ray Serve so a fresh model is hot-reloaded.

    Failures are swallowed — the Ray Serve background poller (Phase 3c) is the
    safety net, so we never block the pipeline on a webhook outage.
    """
    url = os.getenv("RAY_SERVE_RELOAD_URL", "").strip()
    if not url:
        return
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"{url.rstrip('/')}/reload/{model_id}", method="POST")
        urllib.request.urlopen(req, timeout=2.0).close()  # noqa: S310
        print(f"[pipeline] Notified Ray Serve at {url} for model={model_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] Ray Serve webhook to {url} failed (poller will catch up): {exc}")


@task(
    name="promote",
    retries=_IO_RETRIES,
    retry_delay_seconds=_IO_RETRY_DELAYS,
    timeout_seconds=_FETCH_TIMEOUT_S,
)
def promote_task(
    model_name: str,
    registration: dict,
    metrics: dict,
) -> str:
    """Walk the lifecycle rules and set every alias the new version qualifies for.

    Phase 3 lifecycle contract — ``get_inference_params()`` may return either:

    * Legacy form (single Production alias)::

        {
            "promotion_metric": "rmse",
            "promotion_threshold": 50.0,
            "promotion_direction": "lower_is_better",
            ...
        }

    * Multi-stage form::

        {
            "lifecycle": [
                {"name": "Staging",    "metric": "rmse", "threshold": 100.0, "direction": "lower_is_better"},
                {"name": "Canary",     "metric": "rmse", "threshold":  60.0, "direction": "lower_is_better"},
                {"name": "Production", "metric": "rmse", "threshold":  50.0, "direction": "lower_is_better"},
            ],
            ...
        }

    Both forms work. When ``lifecycle`` is omitted, a single-rule list is built
    from the legacy keys for backward compatibility. The previous Production
    version is moved to the ``Archived`` alias whenever a new Production
    version is set.
    """
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    inf = config_cls.get_inference_params()
    model_id = inf["model_id"]

    version = registration.get("version")
    if version is None:
        print("[pipeline] No registered version — skipping promotion.")
        return "Staging"

    rules = inf.get("lifecycle")
    if not rules:
        rules = [
            {
                "name": "Production",
                "metric": inf["promotion_metric"],
                "threshold": inf["promotion_threshold"],
                "direction": inf.get("promotion_direction", "lower_is_better"),
            }
        ]

    client = mlflow.MlflowClient()
    set_aliases: list[str] = []
    highest_status = "Staging"

    # Capture the previous Production alias so we can roll it into Archived.
    previous_production: str | None = None
    try:
        prev = client.get_model_version_by_alias(model_id, "Production")
        previous_production = str(prev.version)
    except Exception:  # noqa: BLE001
        previous_production = None

    for rule in rules:
        stage = rule["name"]
        metric_val = metrics.get(rule["metric"])
        if metric_val is None:
            print(f"[pipeline] {stage}: metric '{rule['metric']}' missing — skipping rule.")
            continue
        if not _evaluate_stage_rule(
            metric_val, rule["threshold"], rule.get("direction", "lower_is_better")
        ):
            op = ">" if rule.get("direction") == "lower_is_better" else "<"
            print(
                f"[pipeline] {stage}: stays at threshold "
                f"({rule['metric']}={metric_val:.4f} {op} threshold={rule['threshold']})"
            )
            continue
        client.set_registered_model_alias(model_id, stage, str(version))
        set_aliases.append(stage)
        highest_status = stage
        print(
            f"[pipeline] Set @{stage} on {model_id} v{version} "
            f"({rule['metric']}={metric_val:.4f}, threshold={rule['threshold']})"
        )

    # Roll the previous Production version into Archived (only when we actually
    # replaced it, only when it isn't the version we just promoted, and only when
    # no other live alias (Canary/Staging) still points at it — otherwise the same
    # version would carry both e.g. @Canary and @Archived, an inconsistent state
    # that alias-based serving would still route as Canary.
    if (
        "Production" in set_aliases
        and previous_production is not None
        and previous_production != str(version)
    ):
        still_referenced = False
        try:
            rm = client.get_registered_model(model_id)
            aliases_map = getattr(rm, "aliases", None) or {}
            if isinstance(aliases_map, dict):
                still_referenced = any(
                    str(v) == previous_production and a != "Archived"
                    for a, v in aliases_map.items()
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[pipeline] Could not check aliases before archiving: {exc}")

        if still_referenced:
            print(
                f"[pipeline] Not archiving {model_id} v{previous_production} — "
                "still referenced by another alias."
            )
        else:
            try:
                client.set_registered_model_alias(model_id, "Archived", previous_production)
                print(f"[pipeline] Archived previous {model_id} v{previous_production}")
            except Exception as exc:  # noqa: BLE001
                print(f"[pipeline] Could not set @Archived on v{previous_production}: {exc}")

    if "Production" in set_aliases:
        _notify_ray_serve(model_id)

    if not set_aliases:
        print("[pipeline] No stage thresholds passed — version stays unaliased (Staging).")
        return "Staging"

    return highest_status


# ── Generic Prefect Flow ───────────────────────────────────────────────────────


@flow(name="training_flow")
def training_flow(
    model_name: str,
    dataset_cls_name: str,
    is_dummy: bool = False,
    backend_name: str | None = None,
) -> dict:
    """
    Generic HPC training flow — works for ANY registered model × dataset.

    Steps:
        1. data_extraction  — instantiate model + build train dataloader
        2. slurm_submit     — submit to HPC (or run inline in mock mode)
        3. slurm_wait       — poll until job reaches terminal state
        4. result_fetch     — load trained estimator, reconstruct model object
        5. evaluate         — RMSE / MAPE / MSE on validation split
        6. log_mlflow       — register model + metrics
        7. promote          — set @Production alias if metric passes threshold

    Args:
        backend_name: pluggable storage backend selector
            (``"zenodo"`` / ``"minio"`` / ``"dataplane"``). ``None`` keeps the
            legacy in-dataset URL flow for backward compatibility.

    After promotion, run  exa serve reload  to update Ray Serve.
    """
    print(f"\n{'=' * 60}")
    print(
        f"  training_flow | {model_name} × {dataset_cls_name}  backend={backend_name or 'legacy'}"
    )
    print(f"{'=' * 60}\n")

    model_init, loader = data_extraction_task(model_name, dataset_cls_name, is_dummy, backend_name)
    job_id, artifact_hint = slurm_submit_task(
        model_init, loader, model_name, dataset_cls_name, is_dummy
    )
    state, artifact_path = slurm_wait_task(job_id, artifact_hint)
    model = result_fetch_task(
        state, artifact_path, model_name, dataset_cls_name, is_dummy, backend_name
    )
    metrics = evaluate_task(model, model_name, dataset_cls_name, is_dummy, backend_name)
    registration = log_mlflow_task(
        model,
        metrics,
        model_name,
        dataset_cls_name,
        job_id=job_id,
        scheduler=_hpc_scheduler_name(),
        backend_name=backend_name,
    )
    status = promote_task(model_name, registration, metrics)

    return {
        "model": model_name,
        "dataset": dataset_cls_name,
        "backend": backend_name,
        "metrics": metrics,
        "mlflow_version": registration.get("version"),
        "mlflow_run_id": registration.get("run_id"),
        "status": status,
    }


# ── Run all registered models ──────────────────────────────────────────────────


def run_all_flows(is_dummy: bool = False, backend_name: str | None = None) -> list[dict]:
    """
    Run training_flow for every auto-discovered model × dataset combination.
    New models appear automatically once a config with MODEL_CLASS is added.
    """
    results = []
    for model_name, (_, config_cls, _) in MODEL_REGISTRY.items():
        for dataset_cls in config_cls.SUPPORTED_DATASETS:
            result = training_flow(
                model_name=model_name,
                dataset_cls_name=dataset_cls.__name__,
                is_dummy=is_dummy,
                backend_name=backend_name,
            )
            results.append(result)
    return results


# ── Helpers ────────────────────────────────────────────────────────────────────


def _resolve_dataset_cls(config_cls: Any, dataset_cls_name: str) -> Any:
    for ds in config_cls.SUPPORTED_DATASETS:
        if ds.__name__ == dataset_cls_name:
            return ds
    raise ValueError(
        f"'{dataset_cls_name}' not in {config_cls.__name__}.SUPPORTED_DATASETS: "
        f"{[d.__name__ for d in config_cls.SUPPORTED_DATASETS]}"
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ExaMLOps Auto-Pipeline Generator")
    parser.add_argument("--model", default=None, help="Model name (default: all)")
    parser.add_argument("--dataset", default=None, help="Dataset class name (default: all)")
    parser.add_argument("--dummy", action="store_true", help="Use dummy data (no Zenodo download)")
    parser.add_argument("--list", action="store_true", help="List all auto-discovered models")
    parser.add_argument(
        "--backend",
        default=None,
        choices=[None, "zenodo", "minio", "dataplane"],
        help="Storage backend for datasets (default: legacy in-dataset Zenodo flow)",
    )
    parser.add_argument(
        "--registry",
        default=None,
        metavar="PATH",
        help="Path to model_registry.yaml (auto-detected if omitted)",
    )
    parser.add_argument(
        "--env",
        default=None,
        metavar="ENV",
        help="Environment overlay name: dev | staging | prod  (loads pipelines/envs/<ENV>.yaml)",
    )
    parser.add_argument(
        "--export-registry",
        action="store_true",
        help="Export current auto-discovered state to model_registry.yaml and exit",
    )
    args = parser.parse_args()

    # ── Export mode ────────────────────────────────────────────────────────────
    if args.export_registry:
        from pathlib import Path as _Path  # noqa: PLC0415

        out = (
            _Path(args.registry)
            if args.registry
            else _REPO_ROOT / "pipelines" / "model_registry.yaml"
        )
        export_registry(MODEL_REGISTRY, out)
        sys.exit(0)

    # ── Apply YAML overlay ─────────────────────────────────────────────────────
    env_path = None
    if args.env:
        env_path = _REPO_ROOT / "pipelines" / "envs" / f"{args.env}.yaml"
    apply_yaml_registry(args.registry, env_path)

    # ── Run / list ─────────────────────────────────────────────────────────────
    if args.list:
        print("\nRegistered models (after YAML overlay):")
        for name, (_, cfg, tasks) in MODEL_REGISTRY.items():
            datasets = [d.__name__ for d in cfg.SUPPORTED_DATASETS]
            steps = list(tasks.keys())
            print(f"  {name}: datasets={datasets}  steps={steps}")
        sys.exit(0)

    if args.model and args.dataset:
        training_flow(args.model, args.dataset, is_dummy=args.dummy, backend_name=args.backend)
    elif args.model:
        _model_cls, config_cls, _extra = MODEL_REGISTRY[args.model]
        for ds_cls in config_cls.SUPPORTED_DATASETS:
            training_flow(
                args.model, ds_cls.__name__, is_dummy=args.dummy, backend_name=args.backend
            )
    else:
        run_all_flows(is_dummy=args.dummy, backend_name=args.backend)
