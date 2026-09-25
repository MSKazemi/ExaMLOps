"""Validate rendered KServe objects against the pinned CRD schemas — offline (ADR 0142 d4).

The schemas are the ``openAPIV3Schema`` of each served CRD version, vendored from exactly one
KServe release under ``kserve_crds/<version>/`` (descriptions stripped — they carry no validation
meaning). Kubernetes *structural* schemas are not general JSON Schema, so this is a small
validator with Kubernetes semantics rather than a JSON-Schema library:

* **unknown fields are errors** — what ``kubectl apply --validate=strict`` does, and the check the
  old hand-written validator lacked (it passed an ``LLMInferenceService`` with a ``spec.predictor``
  the CRD does not have);
* ``x-kubernetes-int-or-string`` accepts an int or a string; ``x-kubernetes-preserve-unknown-fields``
  and ``additionalProperties`` open an object up;
* types, ``required``, ``enum``, ``anyOf``, ``pattern``, length/item/number bounds are checked.

**Not evaluated:** CEL rules (``x-kubernetes-validations``), ``format``, and any ``pattern`` Python's
``re`` cannot compile. Those run in the API server; passing here means *structurally* valid for the
pinned version, not admitted by a cluster's webhooks.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any

_CRD_ROOT = Path(__file__).parent / "kserve_crds"
_GROUP = "serving.kserve.io"
# Kind → the CRD version rendered and pinned (the storage version of each at the pin).
# ClusterStorageContainer (v1alpha1) is the cluster-scoped object the verify-before-load storage
# initializer is registered as (ADR 0142 d3, spec-usar-1 R-SUB-20).
_KINDS = {
    "InferenceService": "v1beta1",
    "LLMInferenceService": "v1alpha2",
    "ClusterStorageContainer": "v1alpha1",
}
# KubeRay (ADR 0015 d1): the dense multi-model Ray path on Kubernetes. Pinned separately — a
# different project with its own release cadence — but validated by the same structural walker.
_KUBERAY_ROOT = Path(__file__).parent / "kuberay_crds"
_KUBERAY_GROUP = "ray.io"
_KUBERAY_KINDS = {"RayService": "v1"}

# DNS-1035 label: what KServe requires of a service name (it becomes a Service/host name).
_NAME = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_LABEL_VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
_LABEL_KEY_NAME = re.compile(r"^([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]$")


@dataclass(frozen=True)
class Pin:
    version: str
    released: date
    window_weeks: int

    @property
    def expires(self) -> date:
        """Last day inside KServe's support window (N and N-1, 8-week cadence ⇒ 16 weeks)."""
        return self.released + timedelta(weeks=self.window_weeks)


def _pin_dir() -> Path:
    dirs = sorted(p for p in _CRD_ROOT.iterdir() if p.is_dir())
    if len(dirs) != 1:
        raise RuntimeError(
            f"exactly one pinned KServe version expected in {_CRD_ROOT}, found {dirs}"
        )
    return dirs[0]


@cache
def current_pin() -> Pin:
    raw = json.loads((_pin_dir() / "PIN.json").read_text())
    return Pin(
        raw["version"], date.fromisoformat(raw["released"]), int(raw["support_window_weeks"])
    )


def pin_window_error(pin: Pin, today: date) -> str | None:
    """A re-pin instruction when ``today`` is past the pin's support window, else ``None``."""
    if today <= pin.expires:
        return None
    return (
        f"KServe pin {pin.version} (released {pin.released}) left its support window on "
        f"{pin.expires}: vendor the CRD schemas of a supported release into kserve_crds/, update "
        "PIN.json and re-run the render fixtures in the same change"
    )


def pinned_kinds() -> list[tuple[str, str]]:
    return [(kind, f"{_GROUP}/{ver}") for kind, ver in _KINDS.items()] + [
        (kind, f"{_KUBERAY_GROUP}/{ver}") for kind, ver in _KUBERAY_KINDS.items()
    ]


def kuberay_pin_dir() -> Path:
    """The one vendored KubeRay release (``kuberay_crds/<version>/``)."""
    dirs = sorted(p for p in _KUBERAY_ROOT.iterdir() if p.is_dir())
    if len(dirs) != 1:
        raise RuntimeError(
            f"exactly one pinned KubeRay version expected in {_KUBERAY_ROOT}, found {dirs}"
        )
    return dirs[0]


@cache
def load_schema(kind: str) -> dict[str, Any]:
    if kind in _KUBERAY_KINDS:
        path = kuberay_pin_dir() / f"{kind}.{_KUBERAY_KINDS[kind]}.json.gz"
    else:
        path = _pin_dir() / f"{kind}.{_KINDS[kind]}.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        schema: dict[str, Any] = json.load(fh)
    return schema


def validate(obj: dict[str, Any]) -> list[str]:
    """Return every structural error in ``obj`` against the pinned schema (empty = valid)."""
    kind = obj.get("kind")
    api = obj.get("apiVersion")
    if (kind, api) not in pinned_kinds():
        return [f"{kind} {api} is not pinned (pinned: {pinned_kinds()})"]
    errors = _validate_metadata(obj.get("metadata"))
    schema = load_schema(str(kind))
    for key in obj:
        if key not in ("apiVersion", "kind", "metadata", "spec", "status"):
            errors.append(f"{key}: unknown field")
    if "spec" not in obj:
        errors.append("spec: required")
    else:
        _walk(obj["spec"], schema["properties"]["spec"], "spec", errors)
    return errors


def _validate_metadata(meta: Any) -> list[str]:
    if not isinstance(meta, dict):
        return ["metadata: must be an object"]
    errors: list[str] = []
    name = meta.get("name")
    if not isinstance(name, str) or len(name) > 63 or not _NAME.match(name):
        errors.append(
            f"metadata.name: {name!r} is not a DNS-1035 label (lowercase, ≤63, starts a-z)"
        )
    for field in ("labels", "annotations"):
        values = meta.get(field, {})
        if not isinstance(values, dict):
            errors.append(f"metadata.{field}: must be a map of strings")
            continue
        for k, v in values.items():
            if not isinstance(v, str):
                errors.append(f"metadata.{field}.{k}: value must be a string")
            elif field == "labels" and (len(v) > 63 or not _LABEL_VALUE.match(v)):
                errors.append(f"metadata.labels.{k}: {v!r} is not a valid label value")
            if not _valid_key(str(k)):
                errors.append(f"metadata.{field}.{k}: invalid key")
    return errors


def _valid_key(key: str) -> bool:
    prefix, _, name = key.rpartition("/")
    return (
        (not prefix or len(prefix) <= 253) and len(name) <= 63 and bool(_LABEL_KEY_NAME.match(name))
    )


def _walk(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if value is None:
        if not schema.get("nullable"):
            errors.append(f"{path}: must not be null")
        return
    if schema.get("x-kubernetes-int-or-string"):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            errors.append(f"{path}: must be an integer or a string")
        return
    if "anyOf" in schema and "type" not in schema:
        branches = []
        for sub in schema["anyOf"]:
            sub_errors: list[str] = []
            _walk(value, sub, path, sub_errors)
            if not sub_errors:
                return
            branches.append(sub_errors)
        errors.append(f"{path}: matches no allowed form ({branches[0][0] if branches else ''})")
        return
    kind = schema.get("type")
    if kind == "object":
        _walk_object(value, schema, path, errors)
    elif kind == "array":
        if not isinstance(value, list):
            errors.append(f"{path}: must be an array")
            return
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        items = schema.get("items") or {}
        for i, item in enumerate(value):
            _walk(item, items, f"{path}[{i}]", errors)
    elif kind == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: must be a string")
        elif "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        elif "pattern" in schema and not _matches(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']!r}")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: must be an integer")
        else:
            _bounds(value, schema, path, errors)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{path}: must be a number")
        else:
            _bounds(value, schema, path, errors)
    elif kind == "boolean" and not isinstance(value, bool):
        errors.append(f"{path}: must be a boolean")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")


def _walk_object(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return
    for req in schema.get("required", []):
        if req not in value:
            errors.append(f"{path}.{req}: required")
    props = schema.get("properties") or {}
    extra = schema.get("additionalProperties")
    open_ended = schema.get("x-kubernetes-preserve-unknown-fields") or extra is True
    for key, sub in value.items():
        if key in props:
            _walk(sub, props[key], f"{path}.{key}", errors)
        elif isinstance(extra, dict):
            _walk(sub, extra, f"{path}.{key}", errors)
        elif not open_ended:
            errors.append(f"{path}.{key}: unknown field")
    if "maxProperties" in schema and len(value) > schema["maxProperties"]:
        errors.append(f"{path}: allows at most {schema['maxProperties']} properties")


def _matches(pattern: str, value: str) -> bool:
    """OpenAPI ``pattern`` is an unanchored search. A pattern Python cannot compile (an RE2
    construct it lacks) is not evaluated — it cannot fail a render it cannot read."""
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return True


def _bounds(value: float, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: below minimum {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        errors.append(f"{path}: above maximum {schema['maximum']}")
