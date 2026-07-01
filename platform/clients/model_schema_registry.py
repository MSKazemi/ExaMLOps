"""YAML-driven per-model feature schema registry for the DataPlane bridge."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_YAML_DIR = _REPO_ROOT / "pipelines" / "models"


class ModelSchemaRegistry:
    """YAML-driven per-model input feature schema for the DataPlane bridge.

    Usage::

        registry = ModelSchemaRegistry()           # scans pipelines/models/
        registry = ModelSchemaRegistry(yaml_dir)   # explicit dir (useful in tests)

        features = registry.build_features("JPCP", hpc_job_v1_msg)
        registry.validate_features("JPCP", features)  # raises ValueError on mismatch
    """

    def __init__(self, yaml_dir: str | Path | None = None) -> None:
        self._yaml_dir = Path(yaml_dir) if yaml_dir is not None else _DEFAULT_YAML_DIR
        self._schemas: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        for path in sorted(self._yaml_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text())
            except Exception as exc:
                log.warning("Failed to load %s: %s", path, exc)
                continue

            if not isinstance(data, dict):
                continue

            model_name = (data.get("name") or "").strip()
            if not model_name:
                continue

            inference = data.get("inference") or {}
            input_schema = inference.get("input_schema") or {}
            if not input_schema:
                log.warning("No inference.input_schema in %s — skipping", path.name)
                continue

            output_schema = inference.get("output_schema") or {}
            output_field = next(iter(output_schema), "")
            task_type = data.get("task_type", "regression")

            inputs = [
                {"name": field, "type": type_str}
                for field, type_str in input_schema.items()
            ]

            self._schemas[model_name.upper()] = {
                "inputs": inputs,
                "output": output_field,
                "task": task_type,
            }

        log.info(
            "ModelSchemaRegistry: loaded %d models: %s",
            len(self._schemas),
            sorted(self._schemas),
        )

    def models(self) -> list[str]:
        """Return all registered model names (uppercase)."""
        return list(self._schemas)

    def build_features(self, model_name: str, msg: object) -> dict[str, Any]:
        """Extract input features from *msg* according to the model's YAML schema."""
        schema = self._schemas.get(model_name.upper())
        if schema is None:
            log.warning(
                "Unknown model %r in ModelSchemaRegistry — using embedding fallback",
                model_name,
            )
            return {"embedding": list(getattr(msg, "embedding", []))}

        features: dict[str, Any] = {}
        for field in schema["inputs"]:
            name = field["name"]
            val = getattr(msg, name, None)
            if val is None:
                raise ValueError(
                    f"Message has no attribute {name!r} required by model {model_name!r}"
                )
            features[name] = list(val) if not isinstance(val, (int, float, str, bool)) else val
        return features

    def validate_features(self, model_name: str, features: dict[str, Any]) -> None:
        """Raise ValueError if *features* are missing or have wrong types for *model_name*."""
        schema = self._schemas.get(model_name.upper())
        if schema is None:
            return

        for field in schema["inputs"]:
            name = field["name"]
            if name not in features:
                raise ValueError(
                    f"Missing required feature {name!r} for model {model_name!r}"
                )
            if isinstance(field["type"], str) and "list" in field["type"] and not isinstance(features[name], list):
                raise ValueError(
                    f"Feature {name!r} must be a list for model {model_name!r}, "
                    f"got {type(features[name]).__name__}"
                )
            if isinstance(field["type"], str) and "list" not in field["type"] and isinstance(features[name], list):
                raise ValueError(
                    f"Feature {name!r} must be a scalar for model {model_name!r}, "
                    f"got list"
                )
