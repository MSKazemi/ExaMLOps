# pipelines/registry_loader.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModelEntry:
    name: str
    model_class_name: str
    config_class_name: str | None
    datasets: list[str]
    backend: str
    dummy: bool
    enabled: bool
    lifecycle: list[dict[str, Any]]
    serve_aliases: list[str]
    prefect: dict[str, Any]


@dataclass
class ResolvedEntry:
    entry: ModelEntry
    model_cls: type
    config_cls: type


_GLOBAL_DEFAULTS: dict[str, Any] = {
    "backend": "zenodo",
    "dummy": False,
    "serve_aliases": ["Production", "Canary", "Staging"],
    "enabled": True,
    "lifecycle": [],
    "prefect": {
        "schedule": "0 2 * * *",
        "work_pool": "default-agent",
        "concurrency_limit": 1,
    },
}


def _apply_defaults(model: dict, defaults: dict) -> dict:
    result = {**model}
    for key, val in defaults.items():
        if key not in result:
            result[key] = val
        elif key == "prefect" and isinstance(val, dict) and isinstance(result[key], dict):
            result[key] = {**val, **result[key]}
    return result


def _merge_model(base: dict, overlay: dict) -> dict:
    result = {**base}
    for key, val in overlay.items():
        if key == "prefect" and isinstance(val, dict) and isinstance(result.get("prefect"), dict):
            result["prefect"] = {**result["prefect"], **val}
        else:
            result[key] = val
    return result


def load_registry(base_path: Path, env_path: Path | None = None) -> list[ModelEntry]:
    """Load base registry and optionally deep-merge an env overlay.

    Returns ALL entries (enabled and disabled) so callers can decide which to use.
    """
    with open(base_path) as fh:
        base_doc = yaml.safe_load(fh) or {}

    base_defaults = {**_GLOBAL_DEFAULTS, **base_doc.get("defaults", {})}
    ordered: dict[str, dict] = {}
    for m in base_doc.get("models", []):
        m = _apply_defaults(m, base_defaults)
        ordered[m["name"]] = m

    if env_path is not None:
        with open(env_path) as fh:
            env_doc = yaml.safe_load(fh) or {}
        env_explicit_defaults = env_doc.get("defaults", {})
        env_defaults = {**base_defaults, **env_explicit_defaults}
        for em in env_doc.get("models", []):
            name = em["name"]
            # Track which fields were explicitly set in the env model
            env_explicit_fields = set(em.keys()) - {"name"}
            if name in ordered:
                # Step 1: merge raw env model fields on top of base so explicit
                # env fields take precedence.
                merged = _merge_model(ordered[name], em)
                # Step 2: apply env-level defaults (from env defaults: section)
                # to fields that were NOT explicitly set in the env model AND
                # have a corresponding env default override vs. the base default.
                for key, env_val in env_explicit_defaults.items():
                    if key not in env_explicit_fields:
                        if key == "prefect" and isinstance(env_val, dict):
                            merged["prefect"] = {**merged.get("prefect", {}), **env_val}
                        else:
                            merged[key] = env_val
                # Step 3: fill in any still-missing required fields.
                ordered[name] = _apply_defaults(merged, env_defaults)
            else:
                # New model from env: apply defaults so all required fields exist.
                ordered[name] = _apply_defaults(em, env_defaults)

    entries = []
    for m in ordered.values():
        entries.append(ModelEntry(
            name=m["name"],
            model_class_name=m.get("model_class", m["name"]),
            config_class_name=m.get("config_class"),
            datasets=m.get("datasets", []),
            backend=m["backend"],
            dummy=bool(m["dummy"]),
            enabled=bool(m["enabled"]),
            lifecycle=m["lifecycle"],
            serve_aliases=m["serve_aliases"],
            prefect=m["prefect"],
        ))
    return entries


def export_registry(model_registry: dict, output_path: Path) -> None:
    """Write the Python MODEL_REGISTRY dict as a valid model_registry.yaml file.

    Does NOT import Prefect or Ray — only stdlib + PyYAML.
    """
    serve_aliases = os.environ.get("RAY_PRELOAD_ALIASES", "Production,Canary,Staging").split(",")

    models = []
    for name, entry in model_registry.items():
        model_cls, config_cls, _ = entry  # unpack tuple (model_cls, config_cls, tasks)

        datasets = [ds.__name__ for ds in config_cls.SUPPORTED_DATASETS]
        lifecycle = config_cls.get_inference_params().get("lifecycle", [])

        model_record: dict[str, Any] = {
            "name": name,
            "model_class": model_cls.__name__,
            "config_class": config_cls.__name__,
            "datasets": datasets,
            "backend": "zenodo",
            "dummy": False,
            "enabled": True,
            "lifecycle": lifecycle,
            "serve_aliases": serve_aliases,
            "prefect": {
                "schedule": "0 2 * * *",
                "deployment_name": f"examlops-{name.lower()}-nightly",
                "work_pool": "default-agent",
                "concurrency_limit": 1,
            },
        }
        models.append(model_record)

    data: dict[str, Any] = {
        "version": "1",
        "defaults": {
            "backend": "zenodo",
            "dummy": False,
            "serve_aliases": serve_aliases,
            "enabled": True,
        },
        "models": models,
    }

    with output_path.open("w") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)


def resolve_entries(entries: list[ModelEntry], model_registry: dict) -> list[ResolvedEntry]:
    """Bridge from YAML ModelEntry objects to Python classes via the registry.

    Raises ValueError if a referenced model or config class cannot be found.

    Indexes model classes by both their Python type __name__ and, when a
    class-level ``__name__`` attribute is present (used by test fakes), that
    value too.  Also indexes by the registry key itself as a final fallback so
    that entries whose ``model_class`` matches the registry key work without
    needing an exact Python class name match.
    """
    cls_by_name: dict[str, Any] = {}
    config_by_name: dict[str, Any] = {}
    for reg_key, v in model_registry.items():
        mcls, ccls = v[0], v[1]
        # Real Python type name (e.g. "JPCPModel")
        cls_by_name[mcls.__name__] = mcls
        # Class-attribute __name__ override used by test fakes (e.g. "FAKE")
        override = vars(mcls).get("__name__")
        if override:
            cls_by_name[override] = mcls
        # Registry key itself (e.g. "JPCP") as a final fallback
        cls_by_name.setdefault(reg_key, mcls)

        config_by_name[ccls.__name__] = ccls
        cfg_override = vars(ccls).get("__name__")
        if cfg_override:
            config_by_name[cfg_override] = ccls
        config_by_name.setdefault(reg_key, ccls)

    resolved: list[ResolvedEntry] = []
    for entry in entries:
        model_cls = cls_by_name.get(entry.model_class_name)
        if model_cls is None:
            raise ValueError(f"Model class '{entry.model_class_name}' not found in registry")

        if entry.config_class_name is not None:
            config_cls = config_by_name.get(entry.config_class_name)
            if config_cls is None:
                raise ValueError(f"Config class '{entry.config_class_name}' not found in registry")
        else:
            config_cls = next(
                (v[1] for v in model_registry.values() if v[0] is model_cls),
                None,
            )
            if config_cls is None:
                raise ValueError(
                    f"No config class found for model class '{entry.model_class_name}' in registry"
                )

        resolved.append(ResolvedEntry(entry=entry, model_cls=model_cls, config_cls=config_cls))

    return resolved
