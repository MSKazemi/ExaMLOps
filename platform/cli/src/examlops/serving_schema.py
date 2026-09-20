"""Per-model input schemas for the serving snapshot (ADR 0123 decision 3).

The control plane compiles the input schema of every aliased model version into the serving
snapshot, so a replica validates a request against configuration it already holds — the reply path
reads no database, MLflow or artifact store.

**Where the schema comes from.** The MLflow *model signature* logged with the version (the
``signature:`` block of the ``MLmodel`` file), read through the MLflow tracking server's artifact
route. That is the one source every deployment of the control plane can reach: the use-case pack's
model YAML (``inference.input_schema``) is not in the control-plane image, and the platform
database holds no schema. A model version is immutable, so its schema is too — it is cached for the
life of the compiler process, like the other version facts.

The snapshot form (a plain dict, JSON-stable, covered by the snapshot digest)::

    {"kind": "columns" | "tensors",
     "inputs": [{"name": "x0", "type": "FP64"}, ...],   # OIP v2 datatypes; BYTES = not numeric
     "width": 4}                                        # features per row, when known

Pure: no I/O here, so the parser and the validator are tested without MLflow.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

# MLflow column types and numpy tensor dtypes → OIP v2 datatypes.
_COLUMN_TYPES = {
    "double": "FP64",
    "float": "FP32",
    "long": "INT64",
    "integer": "INT32",
    "boolean": "BOOL",
    "string": "BYTES",
    "binary": "BYTES",
    "datetime": "BYTES",
}
_TENSOR_TYPES = {
    "float64": "FP64",
    "float32": "FP32",
    "float16": "FP16",
    "int64": "INT64",
    "int32": "INT32",
    "int16": "INT16",
    "int8": "INT8",
    "uint64": "UINT64",
    "uint32": "UINT32",
    "uint16": "UINT16",
    "uint8": "UINT8",
    "bool": "BOOL",
}
OIP_TYPES = frozenset(_COLUMN_TYPES.values()) | frozenset(_TENSOR_TYPES.values())
KINDS = ("columns", "tensors")


def parse_mlmodel(text: str) -> dict[str, Any] | None:
    """The input schema in an ``MLmodel`` file's signature, or ``None`` when it declares none.

    ``None`` is a definite answer ("this version has no signature"), not a failure: anything
    unreadable is also ``None``, because a schema we cannot interpret must not become a gate.
    """
    try:
        import yaml  # noqa: PLC0415

        doc = yaml.safe_load(text)
        raw = (doc or {}).get("signature", {}).get("inputs") if isinstance(doc, dict) else None
        specs = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001 - unreadable == no schema
        return None
    if not isinstance(specs, list) or not specs:
        return None
    inputs: list[dict[str, str]] = []
    width = 0
    tensors = False
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            return None
        if spec.get("type") == "tensor":
            tensors = True
            ts = spec.get("tensor-spec") or {}
            dtype = _TENSOR_TYPES.get(str(ts.get("dtype")))
            shape = ts.get("shape")
            if dtype is None or not isinstance(shape, list):
                return None
            tail = [d for d in shape[1:] if isinstance(d, int) and d > 0]
            width += math.prod(tail) if tail else 1
            inputs.append({"name": str(spec.get("name") or f"input-{i}"), "type": dtype})
        else:
            name, dtype = spec.get("name"), _COLUMN_TYPES.get(str(spec.get("type")))
            if not name or dtype is None:
                return None  # a column we cannot name or type cannot be checked
            width += 1
            inputs.append({"name": str(name), "type": dtype})
    return {"kind": "tensors" if tensors else "columns", "inputs": inputs, "width": width or None}


def normalize(schema: Any) -> dict[str, Any] | None:
    """``schema`` if it is a well-formed snapshot input schema, else ``None`` (never raises).

    The snapshot is digest-verified before it is read, so a malformed schema means a compiler bug
    or a future format; the replica then behaves as if the model had none (fail open, logged by
    the caller) rather than refusing every request for it.
    """
    if not isinstance(schema, dict) or schema.get("kind") not in KINDS:
        return None
    inputs = schema.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return None
    seen: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None
        if item["name"] in seen or item.get("type") not in OIP_TYPES:
            return None
        seen.add(item["name"])
    width = schema.get("width")
    if width is not None and (isinstance(width, bool) or not isinstance(width, int) or width < 1):
        return None
    return schema


_ARTIFACT_PATH = re.compile(r"/artifacts/(.+?)/?$")
_LOGGED_MODEL = re.compile(r"^models:/(m-[0-9a-f]+)$")


def mlmodel_location(source: str | None, run_id: str | None) -> tuple[str, dict[str, str]] | None:
    """``(route, params)`` of the ``MLmodel`` file for a model version, or ``None`` if unknowable.

    A run-logged model lives at ``<run artifact root>/<artifact_path>/MLmodel`` (the tracking
    server's ``/get-artifact``); an MLflow 3 logged model (``models:/m-<id>``) is read through its
    own artifact route.
    """
    if not source:
        return None
    logged = _LOGGED_MODEL.match(source)
    if logged:
        return (
            f"/api/2.0/mlflow/logged-models/{logged.group(1)}/artifacts/files",
            {"artifact_file_path": "MLmodel"},
        )
    found = _ARTIFACT_PATH.search(source)
    if run_id and found:
        return "/get-artifact", {"run_uuid": run_id, "path": f"{found.group(1)}/MLmodel"}
    return None
