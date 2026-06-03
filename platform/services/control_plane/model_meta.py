"""Read-only adapter that surfaces model data from YAML config files.

Reads pipelines/models/*.yaml directly — avoids importing pipeline_generator
which transitively requires torch via the modelzoo configurator.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODELZOO = _REPO_ROOT / "modelzoo"
_MODELS_DIR = _REPO_ROOT / "pipelines" / "models"


@dataclass
class ModelMeta:
    name: str
    task_type: str
    estimator_class: str
    supported_datasets: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    promotion: dict[str, Any]
    path_in_repo: str  # relative to repo root, ends with /
    bundled_images: list[str] = field(default_factory=list)


def _scan_yamls() -> dict[str, dict[str, Any]]:
    """Return {model_name: yaml_dict} for all enabled models."""
    result: dict[str, dict[str, Any]] = {}
    if not _MODELS_DIR.is_dir():
        return result
    for fpath in sorted(_MODELS_DIR.glob("*.yaml")):
        try:
            cfg = yaml.safe_load(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not cfg or not cfg.get("enabled", True):
            continue
        name = cfg.get("name")
        if name:
            result[name] = cfg
    return result


def _find_model_dir(model_class_name: str) -> Path | None:
    """Locate the directory containing the model class by scanning modelzoo source files."""
    tasks_root = _MODELZOO / "seanergys_modelzoo" / "models" / "tasks"
    if not tasks_root.is_dir():
        return None
    needle = f"class {model_class_name}"
    for py_file in sorted(tasks_root.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            if needle in py_file.read_text(encoding="utf-8", errors="ignore"):
                return py_file.parent
        except Exception:
            continue
    return None


def list_model_names() -> list[str]:
    try:
        return sorted(_scan_yamls())
    except Exception:
        return []


def get_model_meta(model_name: str) -> ModelMeta:
    yamls = _scan_yamls()
    if model_name not in yamls:
        raise LookupError(model_name)

    cfg = yamls[model_name]
    model_class_name: str = cfg.get("model_class", model_name)
    task_type: str = cfg.get("task_type", "regression").lower()
    framework: str = cfg.get("framework", "sklearn")

    supported_datasets = [d["name"] for d in cfg.get("datasets", []) if d.get("name")]

    inference_section = cfg.get("inference", {}) or {}
    input_schema: dict[str, Any] = inference_section.get("input_schema", {}) or {}
    output_schema: dict[str, Any] = inference_section.get("output_schema", {}) or {}

    serving_section = cfg.get("serving", {}) or {}
    model_id: str = serving_section.get("model_id") or model_name.lower()

    lifecycle = cfg.get("lifecycle") or []
    # Pull promotion thresholds from the Production lifecycle rule if present.
    prod_rule = next((r for r in lifecycle if r.get("name") == "Production"), None)
    promotion: dict[str, Any] = {
        "model_id": model_id,
        "metric": prod_rule.get("metric") if prod_rule else cfg.get("promotion_metric"),
        "threshold": prod_rule.get("threshold") if prod_rule else cfg.get("promotion_threshold"),
        "direction": (
            prod_rule.get("direction") if prod_rule else cfg.get("promotion_direction", "lower_is_better")
        ),
        "lifecycle": lifecycle,
    }

    estimator_class = f"{framework}.{model_class_name}"

    model_dir = _find_model_dir(model_class_name)
    if model_dir is not None:
        try:
            rel = model_dir.relative_to(_REPO_ROOT).as_posix() + "/"
        except ValueError:
            rel = f"modelzoo/seanergys_modelzoo/models/tasks/{model_class_name.lower()}/"
    else:
        rel = f"modelzoo/seanergys_modelzoo/models/tasks/{model_class_name.lower()}/"

    images: list[str] = []
    if model_dir is not None:
        img_dir = model_dir / "images"
        if img_dir.is_dir():
            images = sorted(p.name for p in img_dir.iterdir() if p.is_file())

    return ModelMeta(
        name=model_name,
        task_type=task_type,
        estimator_class=estimator_class,
        supported_datasets=supported_datasets,
        input_schema=input_schema,
        output_schema=output_schema,
        promotion=promotion,
        path_in_repo=rel,
        bundled_images=images,
    )


def read_readme(model_name: str) -> tuple[str, str]:
    """Return (text, sha256) for the model's README.md, or ('', '') if absent."""
    yamls = _scan_yamls()
    if model_name not in yamls:
        return "", ""
    model_class_name: str = yamls[model_name].get("model_class", model_name)
    model_dir = _find_model_dir(model_class_name)
    if model_dir is None:
        return "", ""
    for candidate in ("README.md", "readme.md"):
        p = model_dir / candidate
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return text, sha
    return "", ""


def read_image(model_name: str, filename: str) -> tuple[bytes, str] | None:
    """Return (bytes, content_type) for a bundled image, or None."""
    if "/" in filename or ".." in filename or filename.startswith("."):
        return None
    yamls = _scan_yamls()
    if model_name not in yamls:
        return None
    model_class_name: str = yamls[model_name].get("model_class", model_name)
    model_dir = _find_model_dir(model_class_name)
    if model_dir is None:
        return None
    p = model_dir / "images" / filename
    if not p.is_file():
        return None
    suffix = p.suffix.lower()
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return p.read_bytes(), content_type
