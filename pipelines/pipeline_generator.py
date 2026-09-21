"""
pipeline_generator — Auto-pipeline generator for ExaMLOps.

At startup, auto-discovers all (model, config) pairs from the active use-case pack (ADR 0094;
default ``usecases/reference``, override with ``EXAMLOPS_USECASE_DIR``) by scanning:
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
import shlex
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
_MODELZOO = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or _REPO_ROOT / "modelzoo")
_PLATFORM = _REPO_ROOT / "platform"
_SLURM_ADAPTER_DIR = _PLATFORM / "infra" / "slurm-adapter"
for _p in (str(_REPO_ROOT), str(_PLATFORM), str(_MODELZOO), str(_SLURM_ADAPTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipelines import usecase as _usecase  # noqa: E402
from pipelines.discovery_utils import retrieve_instances_from_file
from pipelines.model_loader import (  # noqa: E402
    ModelYAMLConfig,
    load_model_yaml,
    scan_model_yamls,
)

# registry_loader has no imports from this module — no circular import risk.
from pipelines.registry_loader import export_registry, load_registry, resolve_entries  # noqa: E402

# ── Use-case framework bindings (ADR 0094) ──────────────────────────────────────
# The active pack (default usecases/reference; override with EXAMLOPS_USECASE_DIR) supplies the
# ML-framework base classes + helpers. The engine resolves them through the loader so it imports
# nothing use-case-specific by name; a different pack swaps the whole binding.
_FRAMEWORK = _usecase.framework()
if TYPE_CHECKING:
    # These two are *values* resolved from the active pack at runtime, so a type checker cannot
    # follow the binding — and it should not pretend to: which concrete base class they name is
    # exactly what a pack is free to change. `Any` is the truthful static type. The annotations
    # below keep the readable names for people, while mypy is told the honest thing.
    SeanergysModel = Any
    SeanergysModelConfiguration = Any
else:
    SeanergysModel = _FRAMEWORK["model_base"]
    SeanergysModelConfiguration = _FRAMEWORK["config_base"]
pipeline_step = _FRAMEWORK["pipeline_step"]  # re-exported for callers  # noqa: F401
_ModelParams = _FRAMEWORK["model_params"]
_Dataloader = _FRAMEWORK["dataloader"]
_DataloaderParams = _FRAMEWORK["dataloader_params"]
_get_backend = _FRAMEWORK["get_backend"]
_adapter_for = _FRAMEWORK["framework_adapter"]
SeanergysModelTask = _FRAMEWORK["model_task"]


def _dataset_store_kwargs(backend_name: str) -> dict[str, str]:
    """Credentials for the *dataset* object store, kept separate from the MLflow artifact store.

    Large-scale datasets can live on a different S3/MinIO instance (e.g. the JSC
    dedicated dataset object store) than the platform MinIO that holds MLflow artifacts and
    models. When the ``EXAMLOPS_DATA_S3_*`` variables are unset the backend keeps
    its legacy resolution (``MLFLOW_S3_ENDPOINT_URL`` + ``AWS_*``), so a single
    shared instance keeps working unchanged.
    """
    if backend_name != "minio":
        return {}
    kwargs: dict[str, str] = {}
    if endpoint := os.getenv("EXAMLOPS_DATA_S3_ENDPOINT"):
        kwargs["endpoint_url"] = endpoint
    if access := os.getenv("EXAMLOPS_DATA_S3_ACCESS_KEY"):
        kwargs["access_key"] = access
    if secret := os.getenv("EXAMLOPS_DATA_S3_SECRET_KEY"):
        kwargs["secret_key"] = secret
    return kwargs


def _dataplane_backend(yaml_cfg: Any, ds_entry: Any) -> Any:
    """ADR 0130 §8: the pinned-snapshot backend for a `backend: dataplane` dataset entry."""
    from pipelines.datasets.dataplane import (  # noqa: PLC0415
        DataplaneBinding,
        DataplaneDatasetBackend,
        pin_for,
    )

    binding = DataplaneBinding.from_yaml(getattr(ds_entry, "dataplane", None))
    if binding is None:
        raise ValueError(
            f"model {yaml_cfg.name} dataset {ds_entry.name} uses backend 'dataplane' but has no "
            "datasets[].dataplane binding — add `dataplane: {source: <name>}` to its YAML"
        )
    pin = pin_for(yaml_cfg.name, ds_entry.name, binding, project=yaml_cfg.project or "")
    return DataplaneDatasetBackend(pin, binding.tables)


def _is_dataplane(backend: str | None) -> bool:
    """``dataplane`` in any case. A miss (``Dataplane``) would fall through to the pack's modelzoo
    dataplane backend — the simulator ADR 0130 says must never serve training data."""
    return str(backend or "").strip().lower() == "dataplane"


def _effective_backend(
    model_name: str | None, dataset_name: str, backend_name: str | None
) -> str | None:
    """The backend a build of (model, dataset) uses: the runtime arg, else the model YAML default.

    Mirrors ``_build_train_components`` (``backend_name or ds_entry.backend``) for callers that see
    only the runtime arg — the gate, the MLflow tagging and the HPC argv.
    """
    if backend_name:
        return backend_name
    if not model_name or model_name not in MODEL_REGISTRY:
        return None
    yaml_cfg = getattr(MODEL_REGISTRY[model_name][1], "_yaml", None)
    if yaml_cfg is None:
        return None
    try:
        return yaml_cfg.dataset(dataset_name).backend
    except Exception:  # noqa: BLE001 - an unknown dataset has no YAML default
        return None


def _run_pin(model_name: str | None, dataset_name: str, backend_name: str | None) -> Any:
    """This run's dataplane pin — only when its effective backend is dataplane (ADR 0130 §8).

    Pins are process-wide, so a pin alone proves nothing about *this* run: a leftover from an
    earlier dataplane run must not turn a minio run's gate, tags or HPC argv into dataplane ones.
    """
    if not _is_dataplane(_effective_backend(model_name, dataset_name, backend_name)):
        return None
    from pipelines.datasets.dataplane import current_pin  # noqa: PLC0415

    return current_pin(model_name, dataset_name)


# ── Model Registry ─────────────────────────────────────────────────────────────
#
# model_name → (model_cls, config_cls, tasks_dict)
#
# Populated automatically by auto_register_all() at module load time.
# Manual register_model() calls are still supported for overrides.

MODEL_REGISTRY: dict[
    str,
    # middle slot holds either a config *class* or a YAMLBackedConfig *instance*
    tuple[type[SeanergysModel], Any, dict[str, Any]],
] = {}


# ── Auto-discovery ─────────────────────────────────────────────────────────────


def discover_models() -> dict[str, type[SeanergysModel]]:
    """
    Scan the active pack's model ``tasks_root`` package (ADR 0094) and return every
    concrete model subclass found, keyed by class name.
    """
    tasks_root = _usecase.tasks_dir()
    found: dict[str, type[SeanergysModel]] = {}
    for py_file in sorted(tasks_root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            classes = retrieve_instances_from_file(py_file, SeanergysModel)
            found.update(classes)
        except Exception as exc:
            print(f"[discovery] Warning: could not scan {py_file.name}: {exc}")
    return found


def discover_configs() -> dict[str, type[SeanergysModelConfiguration]]:
    """
    Scan the pack's model_configs/*.py and return every concrete
    SeanergysModelConfiguration subclass found, keyed by class name.
    """
    configs_dir = _usecase.config_dir()
    found: dict[str, type[SeanergysModelConfiguration]] = {}
    for py_file in sorted(configs_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            classes = retrieve_instances_from_file(py_file, SeanergysModelConfiguration)
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
    model_cls: type[SeanergysModel],
    config_cls: type[SeanergysModelConfiguration],
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
    model_cls: type[SeanergysModel],
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


def make_prefect_tasks_from_model(model_cls: type[SeanergysModel]) -> dict[str, Any]:
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

    # Build SeanergysModelParams from YAML model section
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
    if _is_dataplane(effective_backend) and not is_dummy:
        # ADR 0130: never the pack's modelzoo `dataplane` backend (a dead simulator) — the pinned
        # snapshot. Dummy runs never touch the dataset store.
        ds_kwargs["backend"] = _dataplane_backend(yaml_cfg, ds_entry)
    elif (
        effective_backend and effective_backend != "zenodo" and not _is_dataplane(effective_backend)
    ):
        ds_kwargs["backend"] = _get_backend(
            effective_backend, **_dataset_store_kwargs(effective_backend)
        )
    else:
        ds_kwargs["use_zenodo_url"] = True

    loader_params = _DataloaderParams(batch_size=ds_entry.batch_size)
    dataset = dataset_cls(**ds_kwargs)
    loader = _Dataloader(dataset, **loader_params.to_dict())
    return model, dataset, loader


class YAMLBackedConfig:
    """Drop-in for SeanergysModelConfiguration, backed by a per-model YAML file.

    Stored in MODEL_REGISTRY in the config_cls slot. Implements the same
    interface as SeanergysModelConfiguration so all existing pipeline tasks
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
    base_config_cls: Any,  # a SeanergysModelConfiguration subclass (classmethods called below)
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


def _hpc_train_command(
    model_name: str,
    dataset_cls_name: str,
    *,
    is_dummy: bool,
    backend_name: str | None,
    remote_model: str,
    mlflow_uri: str,
) -> str:
    """The ``slurm_train_script.py`` invocation a real-HPC job runs (ADR 0130 §8).

    A dataplane run forwards its pin — ``--backend dataplane --dataset-revision <rev>`` — so the
    compute node trains on the exact snapshot the gate validated and the MLflow run is tagged with.
    The node then needs only the dataset-store env, not ``platform.db`` or source credentials.
    The pin is consulted only for a non-dummy run whose effective backend is dataplane.

    Every value below reaches a generated bash script that a real scheduler executes on a compute
    node, so each is ``shlex.quote``-d before interpolation — defense in depth against a model or
    dataset name (or any other value threaded through here) that carries shell metacharacters,
    even though today's callers only ever pass registry-validated names.
    """
    remote_repo = os.getenv("EXAMLOPS_HPC_REMOTE_REPO", str(_REPO_ROOT))
    remote_python = os.getenv(
        "EXAMLOPS_HPC_REMOTE_PYTHON", str(Path(remote_repo) / ".venv" / "bin" / "python")
    )
    # Forward --dummy so a real-scheduler smoke test trains on the small dummy split
    # instead of the full dataset (parity with mock mode and the CLI --dummy flag).
    flags = " --dummy" if is_dummy else ""
    pin = None if is_dummy else _run_pin(model_name, dataset_cls_name, backend_name)
    if pin is not None:
        flags += f" --backend dataplane --dataset-revision {shlex.quote(pin.revision)}"
    elif backend_name:
        flags += f" --backend {shlex.quote(backend_name)}"
    return (
        f"{shlex.quote(remote_python)} {shlex.quote(remote_repo)}/pipelines/slurm_train_script.py \\\n"
        f"  --model {shlex.quote(model_name)} \\\n"
        f"  --dataset {shlex.quote(dataset_cls_name)} \\\n"
        f"  --output {shlex.quote(remote_model)} \\\n"
        f"  --mlflow-uri {shlex.quote(mlflow_uri)}{flags}\n"
    )


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
    backend_name: str | None = None,
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

    local_job_dir = Path(adapter.working_dir) / run_uuid
    local_job_dir.mkdir(parents=True, exist_ok=True)
    bash_script = local_job_dir / "run.sh"
    bash_script.write_text(
        "#!/bin/bash\n"
        f"mkdir -p {shlex.quote(remote_dir)}\n"
        + _hpc_train_command(
            model_name,
            dataset_cls_name,
            is_dummy=is_dummy,
            backend_name=backend_name,
            remote_model=remote_model,
            mlflow_uri=mlflow_uri,
        )
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

    Returns a fully-populated SeanergysModel ready for evaluate_task.
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
        hasattr(model, "task_type") and model.task_type == SeanergysModelTask.CLASSIFICATION
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


def _pin_dataset_revision(
    dataset_name: str,
    backend_name: str | None,
    run_id: str,
    *,
    model_name: str | None = None,
    is_dummy: bool = False,
) -> None:
    """A1 (ADR 0003): resolve, tag, and record the dataset revision for this run.

    Fully fail-open (spec R4/R13): any error is swallowed so a revision hiccup can
    never fail a training run. Honours an explicit ``EXAMLOPS_DATASET_REVISION`` pin
    (spec R12) and otherwise records the resolved revision (lakeFS commit or
    ``unknown`` when content can't be materialised in this context).

    A dataplane run (ADR 0130 §8) tags the snapshot it already pinned — never a fresh
    resolution — and links this run to the revision row written at pull time. Only a
    non-dummy run whose effective backend is dataplane does; a dummy run never claims a
    snapshot's first-run link.
    """
    try:
        pin = None if is_dummy else _run_pin(model_name, dataset_name, backend_name)
        if pin is not None:
            from examlops.data.data_assets import link_dataset_revision_run  # noqa: PLC0415

            for k, v in (
                ("dataset_revision", pin.revision),
                ("dataset_backend", "dataplane"),
                ("dataset_uri", pin.manifest_uri),
                ("dataplane.source", pin.source_key),
                ("dataplane.revision", pin.revision),
            ):
                mlflow.set_tag(k, v)
            link_dataset_revision_run("dataplane", pin.source_key, pin.revision, run_id)
            print(
                f"[pipeline] dataset revision pinned: dataplane@{pin.revision} ({pin.source_key})"
            )
            return
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
    is_dummy: bool = False,
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
            _pin_dataset_revision(
                dataset_name, backend_name, run_id, model_name=model_name, is_dummy=is_dummy
            )
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

    if registration.get("version"):
        _sign_registered(registered_model_name, str(registration["version"]))
    _emit_training_lineage(
        registered_model_name,
        dataset_name,
        registration,
        job_id,
        scheduler,
        backend_name,
        model_name=model_name,
        is_dummy=is_dummy,
    )
    return registration


def _sign_registered(model: str, version: str) -> None:
    """Sign the version just registered — the bytes the serving plane will download (P4.10).

    ``EXAMLOPS_SIGN_AT_REGISTRATION``: ``auto`` (default) signs whenever a signing key is
    configured and skips otherwise; ``required`` fails the run when the version cannot be signed,
    so an unsigned version never waits in the registry for a serving plane that enforces
    verification to refuse it; ``off`` never signs. Signing runs after registration: a signature
    needs a version to name.
    """
    policy = os.getenv("EXAMLOPS_SIGN_AT_REGISTRATION", "auto").strip().lower()
    if policy == "off":
        return
    try:
        from examlops import supplychain  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - a worker image without the platform package
        if policy == "required":
            raise RuntimeError(f"cannot sign {model} v{version}: {exc}") from exc
        return
    if supplychain.signing_configured() is None:
        if policy == "required":
            raise RuntimeError(
                f"cannot sign {model} v{version}: no signing key "
                "(EXAMLOPS_SIGNING_PRIVATE_KEY_FILE or secret model-signing/ed25519-private)"
            )
        return
    try:
        sig = supplychain.sign_registered_version(model, version, actor="pipeline")
    except Exception as exc:  # noqa: BLE001 - reported, and fatal only when required
        if policy == "required":
            raise
        print(f"[pipeline] Could not sign {model} v{version}: {exc}")
        return
    print(f"[pipeline] Signed {model} v{version} ({sig.algo}, {sig.digest[:23]})")


def _emit_training_lineage(
    model: str,
    dataset: str,
    registration: dict,
    job_id: str | None,
    scheduler: str | None,
    backend_name: str | None,
    *,
    model_name: str | None = None,
    is_dummy: bool = False,
) -> None:
    """A2 lineage for a completed training run (ADR 0004 clauses 1 and 2). Fail-open.

    This is the emit point the ADR's clause 1 names first and the one that had none. It is also
    the only place that knows the **HPC job id** clause 2 asks for as a facet: everything
    downstream sees a model version, not the scheduler job that produced it, so a facet added
    anywhere else would have had nothing real to carry.

    Emitted after registration so the model node names a resolved version. When registration was
    skipped the version is unknown and the event still records the run — a training run that
    produced no registered version is exactly the case a provenance graph should show.

    ``model`` is the MLflow model id (``jpcp``) the lineage nodes and model fields carry;
    ``model_name`` is the ``MODEL_REGISTRY`` key (the YAML model name, ``JPCP``) the run's
    dataplane pin is found by. They differ, and the YAML default backend is looked up by the
    registry key: with the MLflow id, a run whose YAML defaults to dataplane (``exa retrain``,
    ``POST /retrain``, the autopilot, deployments — none pass ``--backend``) recorded no
    revision. A dummy run trains on synthetic rows and never claims the snapshot.
    """
    try:
        from examlops.lineage import dataset_node, emit_lineage, model_node  # noqa: PLC0415

        run_id, in_flow = _training_lineage_run_id(registration, model, dataset)
        version = registration.get("version")
        # The exact data this run trained on, so `exa models lineage --impact <revision>` can find
        # it: the dataplane pin (ADR 0130 §8), else a revision pinned with --dataset-revision.
        facets: dict[str, Any] = {"backend": backend_name or ""}
        pin = None if is_dummy else _run_pin(model_name or model, dataset, backend_name)
        if pin is not None:
            revision: str | None = pin.revision
            facets["dataplane.source"] = pin.source_key
        else:
            revision = os.getenv("EXAMLOPS_DATASET_REVISION") or None
        emit_lineage(
            # Inside a flow run the flow's hooks send the run's one START and its one ending, as
            # the spec requires; what registration learned (dataset → model) is an OTHER on that
            # run. Called on its own, this is the whole run and so its COMPLETE.
            "OTHER" if in_flow else "COMPLETE",
            job=f"train:{model}",
            run_id=str(run_id),
            inputs=[dataset_node(dataset, revision)],
            outputs=[model_node(model, version or "unregistered")],
            facets=facets,
            dataset_revision=revision,
            mlflow_run_id=registration.get("run_id"),
            model=model,
            model_version=version,
            hpc_job_id=job_id,
            scheduler=scheduler,
        )
    except Exception as exc:  # noqa: BLE001 - lineage must never fail a completed training run
        print(f"[pipeline] lineage emit skipped: {exc}")


def _auto_repro_bundle(
    model_name: str,
    dataset: str,
    registration: dict,
    metrics: dict,
    backend_name: str | None,
    *,
    seed: int | None,
    is_dummy: bool,
) -> None:
    """ADR 0038 cl. 4: build the reproducibility bundle for a registered version. Fail-open.

    Off unless ``EXAMLOPS_REPRO_AUTO_BUNDLE`` is truthy, so the default run is untouched. When
    armed, nothing here can fail the run: ``auto_bundle`` swallows, counts and audits every error,
    and the surrounding ``try`` covers the dataset lookup that precedes it.
    """
    try:
        from examlops.reproducibility import auto  # noqa: PLC0415

        if not auto.enabled() or not registration.get("version"):
            return
        model_id = _lineage_model_id(model_name)
        pin = None if is_dummy else _run_pin(model_name, dataset, backend_name)
        revision: str | None = None
        source: dict[str, Any] | None = None
        if pin is not None:
            revision, source = pin.revision, {"kind": "dataplane", "source_key": pin.source_key}
        elif not is_dummy:
            revision = os.getenv("EXAMLOPS_DATASET_REVISION", "").strip() or None
            if revision is None and registration.get("run_id"):
                tag = mlflow.MlflowClient().get_run(registration["run_id"]).data.tags
                revision = tag.get("dataset_revision") or None
            if revision == "unknown":
                revision = None  # an unresolved revision pins nothing; never record it as one
        auto.auto_bundle(
            model_id,
            registration["version"],
            trigger="training",
            metrics=metrics,
            dataset_name=dataset,
            dataset_revision=revision,
            dataset_source=source,
            seed=seed,
            run_spec={
                "registry_model": model_name,
                "dataset": dataset,
                "backend": backend_name,
                "dummy": bool(is_dummy),
            },
        )
    except Exception as exc:  # noqa: BLE001 - never fail a completed training run
        print(f"[pipeline] reproducibility bundle skipped: {exc}")


def _training_lineage_run_id(registration: dict, model: str, dataset: str) -> tuple[str, bool]:
    """``(run id, inside a flow run)`` for this training run's lineage.

    The id is the Prefect flow run's: one run, one id, for every event it emits — the flow's
    state hooks, which know only the flow run, send ``START`` and the ending, and registration
    sends an ``OTHER`` here. With the MLflow run id here instead, a run's ``START`` and its ending
    were two runs, and a receiver showed every training run as still running. The MLflow run id
    travels as its own facet. Outside a flow run (a direct call, a test) there is no flow run id:
    the MLflow run id stands in, and there are no hooks to end the run.
    """
    try:
        from prefect.runtime import flow_run  # noqa: PLC0415

        flow_run_id = flow_run.id
    except Exception:  # noqa: BLE001 - no Prefect context is "no flow run", not an error
        flow_run_id = None
    if flow_run_id:
        return str(flow_run_id), True
    return str(registration.get("run_id") or f"train-{model}-{dataset}"), False


def _lineage_model_id(model_name: str) -> str:
    """The MLflow model id the training job is named after (``train:<model_id>``)."""
    try:
        return str(MODEL_REGISTRY[model_name][1].get_inference_params()["model_id"])
    except Exception:  # noqa: BLE001 - an unknown model still gets a job name
        return model_name.lower()


def _flow_state_lineage(event_type: str) -> Any:
    """A Prefect flow-state hook that emits this training run's ``event_type`` (ADR 0004 cl. 1).

    OpenLineage asks for exactly one ``START`` and one of ``COMPLETE``/``ABORT``/``FAIL`` per run,
    and the flow's state is the only thing that knows which ending happened: a run that registered
    its version and then failed at promotion ended in ``FAIL``. Hooks rather than a ``try`` around
    the flow body: they see every way a run ends — an exception (``on_failure``), the
    infrastructure dying under it (``on_crashed``), a cancellation (``on_cancellation``) —
    including the ones no ``except`` inside the process can catch.
    """

    def hook(flow: Any, flow_run: Any, state: Any) -> None:
        _emit_flow_state_lineage(event_type, flow_run, state)

    hook.__name__ = f"_lineage_{event_type.lower()}"
    return hook


def _emit_flow_state_lineage(event_type: str, flow_run: Any, state: Any) -> None:
    """``START`` / ``FAIL`` / ``ABORT`` for one training flow run, under its flow run id. Fail-open:
    a hook that raised would turn a lineage problem into a flow-state problem."""
    try:
        from examlops.lineage import dataset_node, emit_lineage, error_facet  # noqa: PLC0415

        params = dict(getattr(flow_run, "parameters", None) or {})
        model_id = _lineage_model_id(str(params.get("model_name") or "unknown"))
        dataset = params.get("dataset_cls_name")
        facets: dict[str, Any] = {}
        if event_type == "FAIL":
            facets.update(error_facet(getattr(state, "message", None) or "training run failed"))
        emit_lineage(
            event_type,
            job=f"train:{model_id}",
            run_id=str(flow_run.id),
            inputs=[dataset_node(str(dataset))] if dataset else [],
            facets=facets,
            model=model_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] lineage {event_type} skipped: {exc}")


def _evaluate_stage_rule(metric_val: float, threshold: float, direction: str) -> bool:
    """Return True iff *metric_val* satisfies *threshold* under *direction*."""
    if direction == "lower_is_better":
        return metric_val <= threshold
    return metric_val >= threshold


def _announce_alias(model_id: str, alias: str, version: Any, previous: str | None) -> None:
    """Publish ``model.alias_changed`` for an alias this run moved (P2.4); best effort.

    A worker image without the platform package still promotes — the serving plane's alias poll
    picks the change up — so an absent package is not an error here.
    """
    try:
        from examlops import events  # noqa: PLC0415
    except ImportError:
        return
    events.alias_changed(
        model_id, alias, version, previous_version=previous, actor="pipeline", via="training-flow"
    )


def _notify_ray_serve(model_id: str) -> None:
    """Best-effort webhook to Ray Serve so a fresh model is hot-reloaded.

    Failures are swallowed — the Ray Serve background poller (Phase 3c) is the
    safety net, so we never block the pipeline on a webhook outage.
    """
    # RAY_SERVE_RELOAD_URL was wired nowhere, so the "reflected in serving within seconds"
    # contract silently never fired anywhere and promotion visibility was solely the 60s
    # poller. Fall back to RAY_SERVE_URL (set in every containerized context) so the webhook
    # actually fires there; unset both ⇒ poller-only, as before.
    url = (os.getenv("RAY_SERVE_RELOAD_URL") or os.getenv("RAY_SERVE_URL") or "").strip()
    if not url:
        return
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"{url.rstrip('/')}/reload/{model_id}", method="POST")
        # /reload is an admin route (plan P0.6); without the token it answers 401/503 and the
        # 60 s alias poller still picks the promotion up.
        admin_token = os.getenv("RAY_SERVE_ADMIN_TOKEN", "")
        if admin_token:
            req.add_header("Authorization", f"Bearer {admin_token}")
        urllib.request.urlopen(req, timeout=2.0).close()  # noqa: S310
        print(f"[pipeline] Notified Ray Serve at {url} for model={model_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] Ray Serve webhook to {url} failed (poller will catch up): {exc}")


def _eval_gate_refusal(
    model_name: str, model_id: str, version: Any, higher_is_better: bool
) -> str | None:
    """Why the ADR 0008 eval gate refuses this version, or None when it does not.

    ``exa pipeline promote`` and the autopilot both run the gate before moving an alias; this
    flow's own promotion did not, so a ``block``-mode gate stopped every road to Production but
    the one most versions take. The decision is ``examlops.evaluation.gate.promotion_refusal``
    (no gate or a ``warn`` gate → None; a failed or unrunnable gate → the reason, audited). A
    worker image without the platform package cannot have a gate configured, so it promotes as
    before.
    """
    try:
        from examlops.evaluation.gate import promotion_refusal
    except ImportError:
        return None
    return promotion_refusal(
        [model_name, model_id],
        str(version),
        higher_is_better=higher_is_better,
        actor=os.getenv("EXAMLOPS_ACTOR") or "pipeline",
        source="pipeline",
    )


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

    # ADR 0008 — the eval gate protects every alias past Staging, the candidate stage the suite
    # evaluates (gating Staging would stop a fresh version from ever being evaluated). It is run
    # once per version, lazily, before the first such alias move — and before the move is
    # announced, so a refused stage is neither set nor published.
    gate_refusal: str | None = None
    gate_checked = False

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
        if stage != "Staging":
            if not gate_checked:
                gate_checked = True
                gate_refusal = _eval_gate_refusal(
                    model_name,
                    model_id,
                    version,
                    rule.get("direction", "lower_is_better") == "higher_is_better",
                )
            if gate_refusal is not None:
                print(f"[pipeline] {stage}: not promoted — {gate_refusal} (ADR 0008).")
                break  # stages are ordered; a refused version goes no further
        client.set_registered_model_alias(model_id, stage, str(version))
        _announce_alias(
            model_id,
            stage,
            version,
            previous_production if stage == "Production" else None,
        )
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
                _announce_alias(model_id, "Archived", previous_production, None)
                print(f"[pipeline] Archived previous {model_id} v{previous_production}")
            except Exception as exc:  # noqa: BLE001
                print(f"[pipeline] Could not set @Archived on v{previous_production}: {exc}")

    if "Production" in set_aliases:
        _notify_ray_serve(model_id)

    if not set_aliases:
        print("[pipeline] No stage thresholds passed — version stays unaliased (Staging).")
        return "Staging"

    return highest_status


# ── A5 data-contract training gate (ADR 0005 clause 2) ─────────────────────────


def _contract_gate_mode() -> str:
    """``enforce`` (default, the ADR's "fails closed") | ``warn`` | ``off``."""
    mode = os.environ.get("EXAMLOPS_DATA_CONTRACT_GATE", "enforce").strip().lower()
    return mode if mode in ("enforce", "warn", "off") else "enforce"


class DataContractViolation(RuntimeError):
    """Raised when an error-severity contract check fails before training (R5)."""


def data_contract_gate(
    dataset_name: str,
    backend_name: str | None,
    *,
    is_dummy: bool = False,
    model_name: str | None = None,
) -> dict:
    """Validate the A1-pinned dataset before training. Fails closed (ADR 0005 clause 2).

    Returns a report describing what happened, including the skips — a gate that records
    nothing when it could not run is indistinguishable from one that passed, which is the
    failure this whole ADR exists to prevent.

    Three things are deliberately **not** violations, and each says so rather than failing:
    a dataset with no contract (most of them), data whose location cannot be read in this
    context (the revision resolver may return a remote or unmaterialised URI), and a
    ``--dummy`` run, whose synthetic rows were never meant to satisfy a production contract.
    A genuine ``error``-severity failure raises :class:`DataContractViolation`.

    A dataplane snapshot is validated ONE table at a time and never concatenated (ADR 0130 final
    review I9): the contract's ``table``, else the tables the model's binding maps, else every
    table in the snapshot, each on its own. Each table is read through the dataplane's bounded
    reader — at most ``EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS`` rows — and the report says so
    (``sampled``, and per table ``rows_checked`` of ``rows``) when a table was cut short.
    """
    report: dict = {"dataset": dataset_name, "gate": _contract_gate_mode()}
    if report["gate"] == "off":
        return {**report, "validated": False, "reason": "gate disabled"}
    if is_dummy:
        return {**report, "validated": False, "reason": "dummy run — synthetic rows"}
    tables: dict[str, dict[str, Any]] = {}
    try:
        from pipelines.contracts import load_contract  # noqa: PLC0415

        contract = load_contract(dataset_name)
        if contract is None:
            return {**report, "validated": False, "reason": "no contract for this dataset"}

        inputs, source = _contract_inputs(
            dataset_name,
            backend_name,
            model_name=model_name,
            table=getattr(contract, "table", None),
        )
        if inputs is None:
            return {**report, "validated": False, "reason": source}

        per_table = []
        for table, load in inputs:  # one table in memory at a time
            sample = load()
            per_table.append((table, contract.validate(sample.frame)))
            tables[table] = {
                "rows_checked": sample.rows,
                "rows": sample.total_rows,
                "sampled": sample.sampled,
            }
            del sample
        result = _combine_table_results(per_table)
    except DataContractViolation:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken gate must not masquerade as a pass
        return {**report, "validated": False, "reason": f"gate error: {exc}"}

    _record_contract_result(dataset_name, result)
    sampled = any(t["sampled"] for t in tables.values())
    report.update(
        {"validated": True, "passed": result.passed, "score": result.score, "sampled": sampled}
    )
    if any(tables):  # a dataplane pin: say which tables were checked, and how much of each
        report["tables"] = tables
    failures = _describe(result.errors)
    if sampled:
        cut = [f"{t} {s['rows_checked']}/{s['rows']}" for t, s in tables.items() if s["sampled"]]
        failures += f" [checked a sample of rows: {', '.join(cut)}]"
    if not result.passed and report["gate"] == "enforce":
        raise DataContractViolation(
            f"{dataset_name} violates its data contract (score {result.score}): {failures}"
        )
    if not result.passed:
        print(f"[contract] {dataset_name} FAILED but gate=warn — continuing: {failures}")
    return report


def _describe(checks: list) -> str:
    """Readable failure text. ``QualityResult.errors`` yields check *dicts*, not strings —
    joining them directly raises a TypeError inside the very path that reports a violation."""
    return "; ".join(f"{c.get('name')} ({c.get('observed')})" for c in checks)


def _combine_table_results(per_table: list[tuple[str, Any]]) -> Any:
    """One verdict over tables validated separately: passed iff every table passed. With several
    tables each check is named ``<table>:<check>`` so a failure says which table failed."""
    if len(per_table) == 1:
        return per_table[0][1]
    from pipelines.contracts import QualityResult  # noqa: PLC0415

    checks = [
        {**c, "name": f"{table}:{c.get('name')}"}
        for table, result in per_table
        for c in result.checks
    ]
    score = round(sum(1 for c in checks if c.get("passed")) / (len(checks) or 1), 4)
    passed = all(result.passed for _, result in per_table)
    # Which engine judged. One process, one engine — unless Pandera faulted on one table and that
    # table fell back, in which case the record says both rather than claiming either.
    engine = "+".join(sorted({getattr(r, "engine", "python") for _, r in per_table}))
    return QualityResult(passed=passed, score=score, checks=checks, engine=engine)


def _dataplane_binding(model_name: str | None, dataset_name: str) -> Any:
    """The model YAML's ``datasets[].dataplane`` binding for (model, dataset), else ``None``."""
    if not model_name or model_name not in MODEL_REGISTRY:
        return None
    yaml_cfg = getattr(MODEL_REGISTRY[model_name][1], "_yaml", None)
    if yaml_cfg is None:
        return None
    try:
        from pipelines.datasets.dataplane import DataplaneBinding  # noqa: PLC0415

        entry = yaml_cfg.dataset(dataset_name)
        return DataplaneBinding.from_yaml(getattr(entry, "dataplane", None))
    except Exception:  # noqa: BLE001 - no YAML / no entry / no binding: every table is checked
        return None


def _contract_inputs(
    dataset_name: str,
    backend_name: str | None,
    *,
    model_name: str | None = None,
    table: str | None = None,
) -> tuple[list[tuple[str, Any]] | None, str]:
    """What the gate validates: ``([(table, load), …], source)``, or ``(None, reason)`` to skip.

    Each ``load()`` returns one table's sample (``frame``, ``rows``, ``total_rows``, ``sampled``)
    and is called only when that table's turn comes, so one table is in memory at a time.

    A dataplane run (ADR 0130 §8) validates the local copy of the snapshot it already pinned —
    only when this run's effective backend is dataplane (the gate never reaches here for a dummy
    run) — table by table (``table``, else the binding's mapped tables, else every table), each
    read through ``examlops.dataplane.pull.read_contract_sample`` and so bounded by
    ``EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS``. A named table the snapshot lacks is validated as
    empty, which fails its checks: a contract for a table the run does not have is not a skip.
    Any other run validates the A1-resolved local copy as one frame (``_contract_dataframe``).
    """
    import functools  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    from pipelines.datasets.versioning import discover_files  # noqa: PLC0415

    pin = _run_pin(model_name, dataset_name, backend_name)
    if pin is not None:
        if not discover_files(pin.local_dir):
            return None, f"no parquet files under {pin.local_dir}"
        from examlops.dataplane.pull import read_contract_sample  # noqa: PLC0415

        if table:
            names = [table]
        else:
            binding = _dataplane_binding(model_name, dataset_name)
            mapped = sorted(set(binding.tables.values())) if binding is not None else []
            names = mapped or list(pin.tables)
        if not names:  # nothing to validate must never read as a pass
            return None, f"{pin.source_key}@{pin.revision[:12]} lists no tables"
        inputs: list[tuple[str, Any]] = []
        for name in names:
            directory = pin.local_dir / name
            parts = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
            inputs.append((name, functools.partial(read_contract_sample, parts)))
        return inputs, str(pin.local_dir)
    df, source = _contract_dataframe(dataset_name, backend_name)
    if df is None:
        return None, source
    whole = SimpleNamespace(frame=df, rows=len(df), total_rows=len(df), sampled=False)
    return [("", lambda: whole)], source


def _contract_dataframe(dataset_name: str, backend_name: str | None) -> tuple[Any, str]:
    """The A1-resolved dataset as one DataFrame, or ``(None, reason)`` — the non-dataplane gate.

    Resolves through the same A1 revision resolver that pins the run, so the gate validates
    **the data this run will train on** rather than whatever happens to be on disk.
    """
    from pipelines.datasets.versioning import discover_files, resolve_revision  # noqa: PLC0415

    rev = resolve_revision(backend_name, dataset_name)
    uri = getattr(rev, "uri", "") or ""
    if not uri or "://" in uri:
        return None, f"revision uri is not a readable local path ({uri or 'unset'})"
    files = discover_files(uri)
    if not files:
        return None, f"no parquet files under {uri}"
    import pandas as pd  # noqa: PLC0415

    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True), uri


def _record_contract_result(dataset_name: str, result: Any) -> None:
    try:
        from examlops.platform_db import record_data_quality_check  # noqa: PLC0415

        record_data_quality_check(dataset_name, result, stage="train", actor="pipeline")
    except Exception as exc:  # noqa: BLE001 - bookkeeping never blocks the gate's verdict
        print(f"[contract] quality-check record skipped: {exc}")


# ── Generic Prefect Flow ───────────────────────────────────────────────────────


@flow(
    name="training_flow",
    on_running=[_flow_state_lineage("START")],
    on_completion=[_flow_state_lineage("COMPLETE")],
    on_failure=[_flow_state_lineage("FAIL")],
    on_crashed=[_flow_state_lineage("FAIL")],
    on_cancellation=[_flow_state_lineage("ABORT")],
)
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
        1b. contract gate   — validate the A1-pinned dataset (A5; fails closed)
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

    # ADR 0130 §8: resolve once per *run*. Every build inside this run shares one dataplane pin,
    # but this run never inherits a pin an earlier run left in the same process.
    from pipelines.datasets.dataplane import forget_pin  # noqa: PLC0415

    forget_pin(model_name, dataset_cls_name)

    # ADR 0038 cl. 4: an EXAMLOPS_SEED, when set, is applied (and later recorded in the bundle).
    from examlops.reproducibility.auto import apply_seed  # noqa: PLC0415

    applied_seed = apply_seed()

    model_init, loader = data_extraction_task(model_name, dataset_cls_name, is_dummy, backend_name)
    gate = data_contract_gate(
        dataset_cls_name, backend_name, is_dummy=is_dummy, model_name=model_name
    )
    print(f"[contract] {gate}")
    job_id, artifact_hint = slurm_submit_task(
        model_init, loader, model_name, dataset_cls_name, is_dummy, backend_name=backend_name
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
        is_dummy=is_dummy,
    )
    status = promote_task(model_name, registration, metrics)
    _auto_repro_bundle(
        model_name,
        dataset_cls_name,
        registration,
        metrics,
        backend_name,
        seed=applied_seed,
        is_dummy=is_dummy,
    )

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
        "--model-yaml",
        default=None,
        metavar="PATH",
        help=(
            "Register one extra per-model YAML (e.g. lowered from a pipeline-as-code IR, ADR 0080) "
            "for this run; it replaces a same-named model. Its config_class shim must exist in "
            "the active pack."
        ),
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
    if args.model_yaml:
        register_model_from_yaml(load_model_yaml(Path(args.model_yaml)))

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
