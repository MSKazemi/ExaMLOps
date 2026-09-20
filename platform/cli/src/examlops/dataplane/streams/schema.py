"""Model input-schema registry for the dataplane stream ingress (ADR 0130/0131, Plan 2).

``ModelSchemaRegistry`` is a **faithful port** of
``platform/clients/model_schema_registry.py`` (which stays in place, unused by anything new, until
release N+1 deletes it): the same error messages verbatim, the same "no ``inference.input_schema``
-> skip this YAML" rule, and the same "a ``None`` value counts as missing" rule for
``build_features``. The only behavioural change is *where* it looks for per-model YAML: the old
module hardcoded ``pipelines/models``; this one asks the platform's use-case seam
(:func:`examlops.usecase.models_dir`), so it follows whatever pack is active instead of one
hardcoded location.

On top of the port, this module adds the payload-side helpers the new stream ingress needs (E3):

* :func:`validate` — reject an oversize payload, or one containing a non-finite float (NaN/Inf)
  anywhere in it, including inside a feature vector.
* :func:`build_body` — build the request body forwarded to the model service: the model's schema
  fields extracted from the inbound payload, plus any ``binding.options["passthrough"]`` keys. The
  passthrough list is what fixes a real defect: the inference pipeline used to 422 on fields (e.g.
  ``num_nodes``) that live outside a model's declared input schema but are still required by the
  service.

These two raise :class:`examlops.dataplane.types.SpecError` (Plan 1's validation-error type) rather
than inventing a new exception hierarchy for the streaming surface.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import yaml

from examlops.dataplane.streams.types import StreamBinding
from examlops.dataplane.types import SpecError
from examlops.usecase import models_dir

log = logging.getLogger(__name__)

#: Recursion depth cap for :func:`validate`'s payload walk — an adversarially deep payload must
#: fail loudly on a bounded check rather than approach Python's own recursion limit.
_MAX_PAYLOAD_DEPTH = 32


class ModelSchemaRegistry:
    """YAML-driven per-model input feature schema for the Dataplane bus bridge.

    Usage::

        registry = ModelSchemaRegistry()           # scans the active pack's models dir
        registry = ModelSchemaRegistry(yaml_dir)   # explicit dir (useful in tests)

        features = registry.build_features("JPCP", hpc_job_v1_msg)
        registry.validate_features("JPCP", features)  # raises ValueError on mismatch
    """

    def __init__(self, yaml_dir: str | Path | None = None) -> None:
        self._yaml_dir = Path(yaml_dir) if yaml_dir is not None else models_dir()
        self._schemas: dict[str, dict[str, Any]] = {}
        #: file name → the model key it contributed on the last scan, so a file that becomes
        #: momentarily unreadable can be recognised as "this model's YAML" (see :meth:`refresh`).
        self._by_file: dict[str, str] = {}
        self._schemas, self._by_file = self._scan()

    def refresh(self) -> None:
        """Re-scan the models directory, replacing what this registry knows (review I2).

        The pack is re-synced at runtime (``StreamSupervisor`` every 60 s), so a registry built
        once at startup would leave a model added afterwards with *no* schema — and a model with
        no schema forwards the payload unchanged, which the inference pipeline then 422s on every
        message, reported as ``validation``, which by design never feeds drift. Nothing would
        fire. The supervisor therefore calls ``StreamIngress.refresh_schema()`` after each
        successful pack sync, which lands here.

        **A refresh may add and change, and it only removes what is genuinely gone.** A file that
        is momentarily unreadable — an editor mid-write, a half-synced pack, a truncated copy —
        keeps the schema it contributed last time, and one WARNING names what was kept. Without
        that, a re-scan could *narrow* what the ingress knows and drop a live model into the
        "forward the payload unchanged" branch, which is the very failure I2 exists to prevent;
        a frozen registry at least could never lose a schema. Only a model whose file has
        disappeared, or whose (readable) YAML no longer declares an ``inference.input_schema``,
        loses its entry.

        The new mapping is built into a local and swapped in with one assignment, so a concurrent
        :meth:`schema_for` sees either the old mapping or the new one, never a half-built one.
        """
        self._schemas, self._by_file = self._scan()

    def _scan(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """Read every model YAML under the registry's directory into a fresh schema mapping, plus
        the file → model index that lets the next scan recognise an unreadable file's model."""
        schemas: dict[str, dict[str, Any]] = {}
        by_file: dict[str, str] = {}
        kept: list[str] = []
        for path in sorted(self._yaml_dir.glob("*.yaml")):
            readable = True
            data: Any = None
            try:
                data = yaml.safe_load(path.read_text())
            except Exception as exc:
                log.warning("Failed to load %s: %s", path, exc)
                readable = False

            if not readable or not isinstance(data, dict):
                # Not a readable document (unparseable, empty, truncated mid-write): keep whatever
                # this file contributed last time rather than silently un-registering a live model.
                previous = self._by_file.get(path.name)
                if previous is not None and previous in self._schemas:
                    schemas[previous] = self._schemas[previous]
                    by_file[path.name] = previous
                    kept.append(previous)
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

            inputs = [{"name": field, "type": type_str} for field, type_str in input_schema.items()]

            schemas[model_name.upper()] = {
                "inputs": inputs,
                "output": output_field,
                "task": task_type,
            }
            by_file[path.name] = model_name.upper()

        if kept:
            log.warning(
                "ModelSchemaRegistry: %d model YAML file(s) could not be read; kept the previous "
                "schema for %s",
                len(kept),
                sorted(set(kept)),
            )
        log.info(
            "ModelSchemaRegistry: loaded %d models: %s",
            len(schemas),
            sorted(schemas),
        )
        return schemas, by_file

    def models(self) -> list[str]:
        """Return all registered model names (uppercase)."""
        return list(self._schemas)

    def schema_for(self, model_name: str) -> dict[str, Any] | None:
        """The internal schema dict for *model_name* (uppercased), or None if unregistered.

        Not part of the ported API — a small accessor the new payload-based helpers below share
        with :meth:`build_features`/:meth:`validate_features` instead of reaching into
        ``_schemas`` directly.
        """
        return self._schemas.get(model_name.upper())

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
                raise ValueError(f"Missing required feature {name!r} for model {model_name!r}")
            if (
                isinstance(field["type"], str)
                and "list" in field["type"]
                and not isinstance(features[name], list)
            ):
                raise ValueError(
                    f"Feature {name!r} must be a list for model {model_name!r}, "
                    f"got {type(features[name]).__name__}"
                )
            if (
                isinstance(field["type"], str)
                and "list" not in field["type"]
                and isinstance(features[name], list)
            ):
                raise ValueError(
                    f"Feature {name!r} must be a scalar for model {model_name!r}, got list"
                )


_registry: ModelSchemaRegistry | None = None


def _default_registry() -> ModelSchemaRegistry:
    """A process-wide registry scanning the active pack's models dir, built lazily."""
    global _registry
    if _registry is None:
        _registry = ModelSchemaRegistry()
    return _registry


def reset_default_registry() -> None:
    """Drop the cached default registry so the next call re-scans the models dir.

    Only needed when the active pack changes at runtime (tests switching ``models_dir()`` via
    ``EXAMLOPS_USECASE_DIR``/``RAY_MODELS_DIR``, or a pack hot-reload); production processes never
    need to call this.
    """
    global _registry
    _registry = None


def _check_payload(value: Any, max_vector_len: int, path: str, depth: int = 0) -> None:
    """Recursively reject a non-finite float, an over-long list, or too-deep nesting under *value*."""
    if depth > _MAX_PAYLOAD_DEPTH:
        raise SpecError(f"payload nested deeper than {_MAX_PAYLOAD_DEPTH} levels")
    if isinstance(value, bool):
        return  # bool is a subclass of int; nothing to check
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SpecError(f"{path} is not finite (NaN/Inf are not allowed)")
    elif isinstance(value, dict):
        for key, sub in value.items():
            _check_payload(sub, max_vector_len, path=f"{path}.{key}", depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > max_vector_len:
            raise SpecError(
                f"{path} has {len(value)} elements, exceeding the {max_vector_len}-element limit"
            )
        for i, sub in enumerate(value):
            _check_payload(sub, max_vector_len, path=f"{path}[{i}]", depth=depth + 1)


def validate(payload: dict[str, Any], max_bytes: int, max_vector_len: int = 65536) -> None:
    """Reject a payload that is oversize, or contains a non-finite float anywhere in it.

    *max_bytes* bounds the JSON-encoded size of *payload*. *max_vector_len* bounds the length of
    any list value (a feature vector, most commonly an embedding). Raises :class:`SpecError` naming
    the offending field.
    """
    encoded = json.dumps(payload)
    size = len(encoded.encode("utf-8"))
    if size > max_bytes:
        raise SpecError(f"payload of {size} bytes exceeds the {max_bytes}-byte limit")
    _check_payload(payload, max_vector_len, path="payload")


def build_body(
    binding: StreamBinding,
    payload: dict[str, Any],
    *,
    registry: ModelSchemaRegistry | None = None,
) -> dict[str, Any]:
    """Build the request body forwarded to the model service.

    Forwards the model's schema fields extracted from *payload*, plus any
    ``binding.options["passthrough"]`` keys present in *payload* — the fix for the inference
    pipeline 422ing on fields (e.g. ``num_nodes``) that a model needs but does not declare in its
    input schema. A schema field missing from *payload*, or present with a ``None`` value, is
    treated as missing and raises :class:`SpecError`. A model with no registered schema forwards
    the payload unchanged (passthrough keys are then already part of it).

    *registry*, when given, wins over the process-wide default (see
    :func:`reset_default_registry`) — a caller holding its own long-lived
    :class:`ModelSchemaRegistry` (e.g. a ``StreamIngress`` constructed against a specific pack, or
    a test pointed at a ``tmp_path``) must never have that silently overridden by whichever pack
    the default registry happened to resolve first.
    """
    active = registry if registry is not None else _default_registry()
    schema = active.schema_for(binding.model)
    if schema is None:
        body: dict[str, Any] = dict(payload)
    else:
        body = {}
        for field in schema["inputs"]:
            name = field["name"]
            val = payload.get(name)
            if val is None:
                raise SpecError(f"Missing required feature {name!r} for model {binding.model!r}")
            body[name] = val

    passthrough = binding.options.get("passthrough")
    if passthrough is not None:
        if not isinstance(passthrough, list) or not all(isinstance(k, str) for k in passthrough):
            raise SpecError(
                f"binding {binding.name!r} options['passthrough'] must be a list of strings, "
                f"got {passthrough!r}"
            )
        for key in passthrough:
            if key in payload:
                body[key] = payload[key]
    return body
