"""Per-model ``resources:`` block in the model YAML — the serving half of ADR 0157 Phase 3.

The model YAML is the single source of truth for a model's serving config (Phase 14). An
optional::

    resources:
      hardware_profile: gpu-small

names a hardware profile (ADR 0157) instead of restating a replica's GPU fraction and CPU count
at the deployment. At deploy time it resolves to ``gpu_fraction``/``cpu`` and flows into
``ray_actor_options`` exactly the way ``autoscale.gpu_fraction`` already does
(:func:`examlops.autoscale.to_ray_deployment_kwargs`) — no second resource vocabulary.

This module deliberately mirrors :mod:`examlops.autoscale.policy_yaml` field for field
(``validate_*_block`` → ``_yaml_blocks`` → per-model lookup), reads the YAML directly through
``examlops.usecase.models_dir`` rather than importing ``pipelines`` (the platform core never
imports the pipeline engine), and is **additive**: a model YAML with no ``resources:`` block
resolves to ``None`` and the deployment keeps exactly the options it has today.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

#: Keys a ``resources:`` block may carry, and the type each must have. Unknown keys are an
#: error rather than a silent no-op — the failure mode a typo'd ``hardware-profile:`` would
#: otherwise have is "the profile was never applied and nothing said so".
KEYS: dict[str, type | tuple[type, ...]] = {
    "hardware_profile": str,
}

__all__ = [
    "KEYS",
    "model_ray_actor_options",
    "validate_resources_block",
    "yaml_block",
    "yaml_blocks",
]


def validate_resources_block(block: Any) -> list[str]:
    """Errors in a YAML ``resources:`` block (empty list = valid).

    Structural only — it never touches the profile registry, so it is usable as a CI guard on a
    machine with no ``platform.db`` (the same stance ``validate_autoscale_block`` takes).
    """
    if block in (None, {}):
        return []
    if not isinstance(block, dict):
        return ["resources must be a mapping"]
    errors = [f"unknown resources key {k!r}" for k in block if k not in KEYS]
    for key, want in KEYS.items():
        if key not in block:
            continue
        val = block[key]
        if isinstance(val, bool) or not isinstance(val, want):
            errors.append(f"resources.{key} has the wrong type ({type(val).__name__})")
    if errors:
        return errors
    if "hardware_profile" not in block:
        return ["resources needs a 'hardware_profile' key (it is the only key it may carry)"]
    if not str(block["hardware_profile"]).strip():
        return ["resources.hardware_profile must not be empty"]
    return []


def yaml_blocks(models_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{model: resources block}`` for every model YAML that declares one (unreadable = skipped)."""
    if models_dir is None:
        from examlops.usecase import models_dir as _md  # noqa: PLC0415

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
            log.warning("resources: cannot read %s", path)
            continue
        block = raw.get("resources") if isinstance(raw, dict) else None
        if block and raw.get("name"):
            out[str(raw["name"])] = block
    return out


def yaml_block(model: str, models_dir: Path | None = None) -> dict[str, Any] | None:
    """The validated ``resources:`` block for ``model`` (case-insensitive), else ``None``."""
    for name, block in yaml_blocks(models_dir).items():
        if name.lower() == model.lower():
            errors = validate_resources_block(block)
            if errors:
                raise ValueError(f"{model}: " + "; ".join(errors))
            return block
    return None


def model_ray_actor_options(
    model: str,
    *,
    models_dir: Path | None = None,
    target_cluster: str | None = None,
) -> dict[str, float] | None:
    """Deploy-time ``ray_actor_options`` for ``model`` from its ``resources.hardware_profile``.

    ``None`` when the model declares no ``resources:`` block — the additive case, where the
    deployment keeps precisely the options it has today.

    The profile must be applicable to ``serving`` (or ``any``); otherwise
    :func:`examlops.hardware_profiles.require_applicability` refuses, naming the profile's actual
    applicability, rather than deploying a shape nobody declared for serving.
    """
    block = yaml_block(model, models_dir)
    if not block:
        return None
    from examlops.hardware_profiles import resolve_for, to_ray_actor_options  # noqa: PLC0415

    name = str(block["hardware_profile"]).strip()
    _profile, resolution = resolve_for(name, "serving", target_cluster=target_cluster)
    return to_ray_actor_options(resolution)
