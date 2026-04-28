"""
pipeline_generator — Auto-pipeline generator for ExaMLOps.

At startup, auto-discovers all (model, config) pairs by scanning:
  • modelzoo/seanergys_modelzoo/models/tasks/**/*.py  → SeanergysModel subclasses
  • pipelines/model_configs/*.py                      → SeanergysModelConfiguration subclasses

Each config class must declare MODEL_CLASS = <ModelClass> to be matched.
Matched pairs are registered automatically — no manual wiring needed.

Then runs a generic Prefect training flow for every (model × dataset):

    train → evaluate → MLflow log → promote to Production

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

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type

import mlflow
import mlflow.sklearn
import numpy as np
from prefect import flow, task
from prefect.cache_policies import NO_CACHE

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ci.utils import retrieve_instances_from_file
from seanergys_modelzoo.models.common.seanergys_configurator import SeanergysModelConfiguration
from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel
from seanergys_modelzoo.decorators import pipeline_step  # noqa: F401 — re-exported for callers

# ── Model Registry ─────────────────────────────────────────────────────────────
#
# model_name → (model_cls, config_cls, tasks_dict)
#
# Populated automatically by auto_register_all() at module load time.
# Manual register_model() calls are still supported for overrides.

MODEL_REGISTRY: Dict[
    str,
    Tuple[Type[SeanergysModel], Type[SeanergysModelConfiguration], Dict[str, Any]],
] = {}


# ── Auto-discovery ─────────────────────────────────────────────────────────────

def discover_models() -> Dict[str, Type[SeanergysModel]]:
    """
    Scan modelzoo/seanergys_modelzoo/models/tasks/**/*.py and return every
    concrete SeanergysModel subclass found, keyed by class name.
    """
    tasks_root = _MODELZOO / "seanergys_modelzoo" / "models" / "tasks"
    found: Dict[str, Type[SeanergysModel]] = {}
    for py_file in sorted(tasks_root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            classes = retrieve_instances_from_file(py_file, SeanergysModel)
            found.update(classes)
        except Exception as exc:
            print(f"[discovery] Warning: could not scan {py_file.name}: {exc}")
    return found


def discover_configs() -> Dict[str, Type[SeanergysModelConfiguration]]:
    """
    Scan pipelines/model_configs/*.py and return every concrete
    SeanergysModelConfiguration subclass found, keyed by class name.
    """
    configs_dir = _REPO_ROOT / "pipelines" / "model_configs"
    found: Dict[str, Type[SeanergysModelConfiguration]] = {}
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
    """
    Discover all config classes in pipelines/model_configs/, match each to its
    model via MODEL_CLASS, and register matched pairs.

    A config that has no MODEL_CLASS declared is skipped with a warning.
    A model already in MODEL_REGISTRY is not re-registered (manual calls win).
    """
    all_configs = discover_configs()
    for config_name, config_cls in all_configs.items():
        model_cls = getattr(config_cls, "MODEL_CLASS", None)
        if model_cls is None:
            print(f"[discovery] Skipping {config_name}: MODEL_CLASS not declared")
            continue
        if model_cls.__name__ in MODEL_REGISTRY:
            continue
        register_model(model_cls, config_cls)
        datasets = [d.__name__ for d in config_cls.SUPPORTED_DATASETS]
        print(f"[discovery] Registered {model_cls.__name__} ← {config_name}  datasets={datasets}")


# ── Registration ───────────────────────────────────────────────────────────────

def register_model(
    model_cls: Type[SeanergysModel],
    config_cls: Type[SeanergysModelConfiguration],
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
    model_cls: Type[SeanergysModel],
    config_cls: Type[SeanergysModelConfiguration],
) -> Dict[str, Any]:
    """
    Scan model_cls for @pipeline_step methods and return a dict of
    Prefect Task callables, each closing over config_cls.

    Standard steps:
      train_step    (model_name, dataset_cls_name, is_dummy) → (model, history)
      evaluate_step (model, model_name, dataset_cls_name, is_dummy) → metrics dict
    """
    step_attrs: Dict[str, Any] = {
        attr: getattr(model_cls, attr)
        for attr in dir(model_cls)
        if callable(getattr(model_cls, attr, None))
        and getattr(getattr(model_cls, attr, None), "_is_pipeline_step", False)
    }

    tasks: Dict[str, Any] = {}

    if "train_step" in step_attrs:
        retries = step_attrs["train_step"]._step_retries

        def _make_train(cfg_cls: Any, rt: int) -> Any:
            @task(name=f"{model_cls.__name__}_train", retries=rt)
            def _train(
                model_name: str,
                dataset_cls_name: str,
                is_dummy: bool = False,
            ) -> Tuple[Any, dict]:
                ds_cls = _resolve_dataset_cls(cfg_cls, dataset_cls_name)
                model, _, loader = cfg_cls.get_train_components(
                    ds_cls, split="train", is_dummy=is_dummy
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
                    ds_cls, split="validation", is_dummy=is_dummy
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


def make_prefect_tasks_from_model(model_cls: Type[SeanergysModel]) -> Dict[str, Any]:
    """
    Public utility: scan a model class for @pipeline_step methods and return
    a dict mapping step attribute name → Prefect Task callable.

    Unlike _build_model_tasks, tasks returned here accept
    (model_instance, *args, **kwargs) directly — useful for custom flows.
    """
    discovered: Dict[str, Any] = {}
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


# ── Auto-register at import time ───────────────────────────────────────────────

auto_register_all()

# Manual overrides — uncomment to force a specific pairing regardless of
# MODEL_CLASS on the config:
#   register_model(MACK, MACKConfiguration)


# ── Infrastructure tasks (not model-specific) ──────────────────────────────────

@task(name="log_mlflow", cache_policy=NO_CACHE)
def log_mlflow_task(
    model: Any,
    metrics: dict,
    model_name: str,
    dataset_name: str,
) -> dict:
    """Log model + metrics to MLflow. Returns registration dict."""
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    inference_params = config_cls.get_inference_params()
    registered_model_name = inference_params["model_id"]

    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
    mlflow.set_experiment(f"{model_name.lower()}_{dataset_name.lower()}")

    registration = {"version": None, "run_id": None, "status": "Staging"}

    with mlflow.start_run(run_name=f"{model_name}_{dataset_name}"):
        mlflow.log_param("model_name", model_name)
        mlflow.log_param("dataset", dataset_name)
        mlflow.log_param("estimator", type(model.estimator).__name__)
        if model.metadata and model.metadata.hyperparameters:
            mlflow.log_params(model.metadata.hyperparameters)
        mlflow.log_metrics(metrics)

        try:
            mlflow.sklearn.log_model(
                sk_model=model.estimator,
                name="model",
                registered_model_name=registered_model_name,
            )
            run_id = mlflow.active_run().info.run_id
            registration["run_id"] = run_id
            client = mlflow.MlflowClient()
            versions = client.search_model_versions(f"run_id='{run_id}'")
            if versions:
                registration["version"] = int(versions[0].version)
                print(f"[pipeline] Registered {registered_model_name} v{registration['version']}")
        except Exception as exc:
            print(f"[pipeline] MLflow registration skipped: {exc}")

    return registration


@task(name="promote")
def promote_task(
    model_name: str,
    registration: dict,
    metrics: dict,
) -> str:
    """Promote model to Production if the promotion metric meets the threshold."""
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    inf = config_cls.get_inference_params()

    version = registration.get("version")
    if version is None:
        print("[pipeline] No registered version — skipping promotion.")
        return "Staging"

    metric_val = metrics.get(inf["promotion_metric"])
    if metric_val is None:
        print(f"[pipeline] Metric '{inf['promotion_metric']}' missing — skipping promotion.")
        return "Staging"

    lower_is_better = inf.get("promotion_direction", "lower_is_better") == "lower_is_better"
    passes = (
        metric_val <= inf["promotion_threshold"]
        if lower_is_better
        else metric_val >= inf["promotion_threshold"]
    )

    if passes:
        client = mlflow.MlflowClient()
        client.set_registered_model_alias(inf["model_id"], "Production", str(version))
        print(
            f"[pipeline] Promoted {inf['model_id']} v{version} → Production "
            f"({inf['promotion_metric']}={metric_val:.4f} threshold={inf['promotion_threshold']})"
        )
        return "Production"

    op = ">" if lower_is_better else "<"
    print(
        f"[pipeline] Stays Staging ({inf['promotion_metric']}={metric_val:.4f} "
        f"{op} threshold={inf['promotion_threshold']})"
    )
    return "Staging"


# ── Generic Prefect Flow ───────────────────────────────────────────────────────

@flow(name="training_flow")
def training_flow(
    model_name: str,
    dataset_cls_name: str,
    is_dummy: bool = False,
) -> dict:
    """
    Generic training flow — works for ANY registered model × dataset.

    Dispatches train and evaluate through model-specific tasks built at
    registration time from @pipeline_step methods on the model class.

    Steps:
        1. Train on training split       (model-specific task from registry)
        2. Evaluate on validation split  (model-specific task from registry)
        3. Log model + metrics to MLflow (shared infrastructure task)
        4. Promote to Production if metric passes threshold (shared)
    """
    print(f"\n{'='*60}")
    print(f"  training_flow | {model_name} × {dataset_cls_name}")
    print(f"{'='*60}\n")

    _, _, tasks = MODEL_REGISTRY[model_name]

    model, history = tasks["train_step"](model_name, dataset_cls_name, is_dummy)
    metrics = tasks["evaluate_step"](model, model_name, dataset_cls_name, is_dummy)
    registration = log_mlflow_task(model, metrics, model_name, dataset_cls_name)
    status = promote_task(model_name, registration, metrics)

    return {
        "model": model_name,
        "dataset": dataset_cls_name,
        "metrics": metrics,
        "mlflow_version": registration.get("version"),
        "mlflow_run_id": registration.get("run_id"),
        "status": status,
    }


# ── Run all registered models ──────────────────────────────────────────────────

def run_all_flows(is_dummy: bool = False) -> list[dict]:
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
    args = parser.parse_args()

    if args.list:
        print("\nAuto-discovered models:")
        for name, (_, cfg, tasks) in MODEL_REGISTRY.items():
            datasets = [d.__name__ for d in cfg.SUPPORTED_DATASETS]
            steps = list(tasks.keys())
            print(f"  {name}: datasets={datasets}  steps={steps}")
        sys.exit(0)

    if args.model and args.dataset:
        training_flow(args.model, args.dataset, is_dummy=args.dummy)
    elif args.model:
        _, config_cls, _ = MODEL_REGISTRY[args.model]
        for ds_cls in config_cls.SUPPORTED_DATASETS:
            training_flow(args.model, ds_cls.__name__, is_dummy=args.dummy)
    else:
        run_all_flows(is_dummy=args.dummy)
