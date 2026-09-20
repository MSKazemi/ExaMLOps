"""Per-model ``autoscale:`` block in the model YAML — the *default* policy (ADR 0031 clause 1).

The model YAML is the single source of truth for a model's serving config; an ``autoscale:`` block
declares min/max/target/scale-to-zero there. The ``autoscale_config`` table (``exa serve autoscale
set``) is an **override**: a model with a DB row uses the row, a model with only a YAML block uses
the block, a model with neither has no policy. No YAML in the shipped packs carries the block, so
behaviour is unchanged until a pack opts in.

This module reads the YAML directly (``examlops.usecase.models_dir``) rather than importing
``pipelines`` — the platform core never imports the pipeline engine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

METRICS = ("rps", "queue_depth", "gpu_util", "p95")
KEYS: dict[str, type | tuple[type, ...]] = {
    "min_replicas": int,
    "max_replicas": int,
    "target_metric": str,
    "target_value": (int, float),
    "scale_to_zero_after_s": int,
    "warm_pool": int,
    "stabilization_s": int,
    "cooldown_s": int,
    "gpu_fraction": (int, float),
}
_DEFAULTS: dict[str, Any] = {
    "tenant": "default",
    "min_replicas": 1,
    "max_replicas": 4,
    "target_metric": "queue_depth",
    "target_value": 10.0,
    "scale_to_zero_after_s": 0,
    "warm_pool": 0,
    "stabilization_s": 30,
    "cooldown_s": 60,
    "gpu_fraction": 1.0,
    "target_ongoing": 8,
    "enabled": 1,
}


def validate_autoscale_block(block: Any) -> list[str]:
    """Errors in a YAML ``autoscale:`` block (empty list = valid)."""
    if block in (None, {}):
        return []
    if not isinstance(block, dict):
        return ["autoscale must be a mapping"]
    errors = [f"unknown autoscale key {k!r}" for k in block if k not in KEYS]
    for key, want in KEYS.items():
        if key not in block:
            continue
        val = block[key]
        if isinstance(val, bool) or not isinstance(val, want):
            errors.append(f"autoscale.{key} has the wrong type ({type(val).__name__})")
    if errors:
        return errors
    if block.get("target_metric", "queue_depth") not in METRICS:
        errors.append(f"autoscale.target_metric must be one of {', '.join(METRICS)}")
    merged = {**_DEFAULTS, **block}
    if merged["min_replicas"] < 0 or merged["max_replicas"] < 1:
        errors.append("autoscale replicas must be min>=0, max>=1")
    if merged["min_replicas"] > merged["max_replicas"]:
        errors.append("autoscale.min_replicas exceeds max_replicas")
    if merged["target_value"] <= 0:
        errors.append("autoscale.target_value must be > 0")
    if merged["scale_to_zero_after_s"] < 0 or merged["warm_pool"] < 0:
        errors.append("autoscale.scale_to_zero_after_s / warm_pool must be >= 0")
    if not 0 < merged["gpu_fraction"] <= 1:
        errors.append("autoscale.gpu_fraction must be in (0, 1]")
    if merged["scale_to_zero_after_s"] > 0 and merged["min_replicas"] > 0:
        errors.append("autoscale scale_to_zero_after_s needs min_replicas 0")
    return errors


def config_from_block(model: str, block: dict[str, Any]) -> dict[str, Any]:
    """A ``autoscale_config``-shaped row for ``model`` (``source`` says where it came from)."""
    errors = validate_autoscale_block(block)
    if errors:
        raise ValueError(f"{model}: " + "; ".join(errors))
    return {"model": model, **_DEFAULTS, **block, "source": "yaml"}


def _yaml_blocks(models_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{model: autoscale block}`` for every model YAML that declares one (unreadable = skipped)."""
    if models_dir is None:
        from examlops.usecase import models_dir as _md

        models_dir = _md()
    out: dict[str, dict[str, Any]] = {}
    if not models_dir.is_dir():
        return out
    for path in sorted(models_dir.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            log.warning("autoscale: cannot read %s", path)
            continue
        block = raw.get("autoscale") if isinstance(raw, dict) else None
        if block and raw.get("name"):
            out[str(raw["name"])] = block
    return out


def yaml_config(model: str, models_dir: Path | None = None) -> dict[str, Any] | None:
    for name, block in _yaml_blocks(models_dir).items():
        if name.lower() == model.lower():
            return config_from_block(model, block)
    return None


def effective_config(model: str, models_dir: Path | None = None) -> dict[str, Any] | None:
    """DB override if present, else the YAML default, else None."""
    from examlops.data.serving import get_autoscale_config

    row = get_autoscale_config(model)
    if row:
        return {**row, "source": "db"}
    return yaml_config(model, models_dir)


def effective_configs(models_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every model's effective policy: DB rows, plus YAML-only models (DB wins per model)."""
    from examlops.data.serving import list_autoscale_configs

    rows = [{**r, "source": "db"} for r in list_autoscale_configs()]
    have = {str(r["model"]).lower() for r in rows}
    for name, block in _yaml_blocks(models_dir).items():
        if name.lower() in have:
            continue
        try:
            rows.append(config_from_block(name, block))
        except ValueError as exc:
            log.warning("autoscale: ignoring invalid YAML policy: %s", exc)
    return sorted(rows, key=lambda r: str(r["model"]))
