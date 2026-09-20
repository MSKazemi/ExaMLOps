"""Open Inference Protocol v2 (REST) for the predictive server (plan P4.5, ADRs 0126 and 0141).

The platform's own ``POST /predict/{model}`` takes a feature dict, flattens it into one float row and
answers one number. KServe, Triton, MLServer and every client written for them speak OIP v2 instead
— named, typed, shaped tensors — and the KServe manifests the platform renders already declare
``protocolVersion: v2``. This module is the protocol half: it turns an OIP v2 request body into the
2-D array a model predicts on, validated against the model's signature *before* the model runs, and
turns the prediction back into an OIP v2 response. The routes live on ``MultiModelServer`` and share
its execution path with ``/predict`` (timeouts, deadlines, metrics, shadow mirroring).

Tabular models take either:

* **one tensor** of shape ``[N, F]`` (or ``[F]`` for a single row) — rows of features; or
* **one tensor per feature column**, shape ``[N]`` or ``[N, k]``, named as in the model signature.
  With a signature the columns are put in the signature's order and every one must be present; without
  one they are taken in request order.

Errors are ``{"error": "<message>"}`` with a 4xx/5xx status, as the protocol specifies.
Pure and dependency-light (numpy only), so it is tested without Ray.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

# OIP v2 tensor datatypes → numpy. BYTES is valid OIP but not a numeric feature.
_NUMERIC = {
    "BOOL": np.bool_,
    "UINT8": np.uint8,
    "UINT16": np.uint16,
    "UINT32": np.uint32,
    "UINT64": np.uint64,
    "INT8": np.int8,
    "INT16": np.int16,
    "INT32": np.int32,
    "INT64": np.int64,
    "FP16": np.float16,
    "FP32": np.float32,
    "FP64": np.float64,
}
# MLflow column types → OIP datatypes, for the metadata a client reads before it sends.
_MLFLOW_TYPES = {
    "double": "FP64",
    "float": "FP32",
    "long": "INT64",
    "integer": "INT32",
    "boolean": "BOOL",
    "string": "BYTES",
    "binary": "BYTES",
    "datetime": "BYTES",
}


class ProtocolError(Exception):
    """A request the protocol refuses; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Signature:
    """What the model says it takes: column names and datatypes, and the feature width."""

    names: tuple[str, ...]
    datatypes: tuple[str, ...]
    width: int | None  # total features per row, when every column's width is known
    columns: bool = False  # column-based (ColSpec): the model expects a DataFrame with these names


def signature_of(model: Any) -> Signature | None:
    """The input signature an MLflow pyfunc model carries, or None when it has none."""
    try:
        schema = model.metadata.get_input_schema()
    except Exception:  # noqa: BLE001 - a model without metadata simply has no signature
        return None
    if schema is None:
        return None
    names: list[str] = []
    types: list[str] = []
    width = 0
    columns = True
    for spec in getattr(schema, "inputs", []) or []:
        name = getattr(spec, "name", None)
        if not name:
            return None  # an unnamed tensor spec: columns cannot be matched by name
        names.append(str(name))
        shape = getattr(spec, "shape", None)
        if shape is not None:  # a TensorSpec
            columns = False
            dtype = getattr(getattr(spec, "type", None), "name", "float64")
            types.append(
                {"float64": "FP64", "float32": "FP32", "int64": "INT64"}.get(dtype, "FP64")
            )
            tail = [d for d in list(shape)[1:] if d and d > 0]
            width = width + (math.prod(tail) if tail else 1)
        else:  # a ColSpec: one scalar per row
            types.append(_MLFLOW_TYPES.get(str(getattr(spec.type, "name", spec.type)), "FP64"))
            width += 1
    return Signature(tuple(names), tuple(types), width or None, columns) if names else None


def signature_from_schema(schema: Any) -> Signature | None:
    """The :class:`Signature` a serving-snapshot input schema describes, or None.

    None when there is no schema or it is malformed: the caller then falls back to the loaded
    model's own signature, so a schema the replica cannot interpret never becomes a gate.
    """
    from examlops.serving_schema import normalize  # noqa: PLC0415

    valid = normalize(schema)
    if valid is None:
        return None
    inputs = valid["inputs"]
    return Signature(
        tuple(i["name"] for i in inputs),
        tuple(i["type"] for i in inputs),
        valid.get("width"),
        valid["kind"] == "columns",
    )


def _finite_number(value: Any) -> bool:
    """A JSON number numpy can take as a float (a bool counts; a string or NaN does not)."""
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def check_features(features: Any, signature: Signature) -> None:
    """Refuse a ``/predict`` feature dict that does not fit a snapshot input schema (status 422).

    The message names the offending field. Extra keys are ignored, as ``/predict`` always has: a
    client that sends more than the model takes is not wrong, only chatty. Columns typed ``BYTES``
    are not numeric features this server can take and are not type-checked here.
    """
    if not isinstance(features, dict):
        raise ProtocolError("features must be an object", 422)
    if signature.columns:
        missing = [n for n in signature.names if n not in features]
        if missing:
            raise ProtocolError(
                f"missing features {missing}; the model takes {list(signature.names)}", 422
            )
        for name, dtype in zip(signature.names, signature.datatypes, strict=True):
            value = features[name]
            if dtype == "BYTES":
                continue
            if _finite_number(value):
                continue
            raise ProtocolError(
                f"feature '{name}' must be a finite number ({dtype}), got {value!r}", 422
            )
        return
    count = 0
    for name, value in features.items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if not _finite_number(item):
                raise ProtocolError(f"feature '{name}' must be numeric, got {item!r}", 422)
        count += len(items)
    if signature.width is not None and count != signature.width:
        raise ProtocolError(
            f"the model takes {signature.width} values per row; the request has {count}", 422
        )


def model_input(array: np.ndarray, signature: Signature | None) -> Any:
    """What to hand the model: a DataFrame with the signature's column names for a column-based
    signature (MLflow enforces the names), the plain array otherwise."""
    if signature is not None and signature.columns and array.shape[1] == len(signature.names):
        import pandas as pd  # noqa: PLC0415 - mlflow's own dependency, present wherever models load

        return pd.DataFrame(array, columns=list(signature.names))
    return array


def _tensor(item: Any, index: int) -> tuple[str, list[int], np.ndarray]:
    if not isinstance(item, dict):
        raise ProtocolError(f"inputs[{index}] must be an object")
    name = item.get("name")
    shape = item.get("shape")
    datatype = item.get("datatype")
    if not isinstance(name, str) or not name:
        raise ProtocolError(f"inputs[{index}].name is required")
    if not isinstance(shape, list) or not all(isinstance(d, int) and d >= 0 for d in shape):
        raise ProtocolError(f"input '{name}': shape must be a list of non-negative integers")
    if datatype == "BYTES":
        raise ProtocolError(f"input '{name}': BYTES is not a numeric feature this model can take")
    if datatype not in _NUMERIC:
        raise ProtocolError(f"input '{name}': unknown datatype {datatype!r}")
    try:
        data: np.ndarray = np.asarray(item.get("data"), dtype=_NUMERIC[datatype]).ravel()
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"input '{name}': data is not {datatype}: {exc}") from exc
    if data.size != math.prod(shape):
        raise ProtocolError(
            f"input '{name}': shape {shape} holds {math.prod(shape)} values, data has {data.size}"
        )
    return name, shape, data.reshape(shape).astype(np.float64)


def to_array(body: Any, signature: Signature | None) -> np.ndarray:
    """The ``[N, F]`` float array an OIP v2 request body describes, validated. Raises ProtocolError."""
    if not isinstance(body, dict):
        raise ProtocolError("the request body must be a JSON object")
    inputs = body.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise ProtocolError("'inputs' must be a non-empty list of tensors")
    tensors = [_tensor(item, i) for i, item in enumerate(inputs)]

    if len(tensors) == 1 and (signature is None or tensors[0][0] not in signature.names):
        # One tensor of rows: [N, F], or [F] for a single row.
        _, shape, data = tensors[0]
        if len(shape) == 1:
            array = data.reshape(1, -1)
        elif len(shape) == 2:
            array = data
        else:
            raise ProtocolError(
                f"input '{tensors[0][0]}': expected shape [N, F] or [F], got {shape}"
            )
    else:
        array = _columns(tensors, signature)

    if array.shape[0] == 0:
        raise ProtocolError("the request has no rows")
    if signature is not None and signature.width is not None and array.shape[1] != signature.width:
        raise ProtocolError(
            f"the model takes {signature.width} features per row; the request has {array.shape[1]}"
        )
    return array


def _columns(tensors: list[tuple[str, list[int], np.ndarray]], signature: Signature | None):
    by_name: dict[str, np.ndarray] = {}
    for name, shape, data in tensors:
        if name in by_name:
            raise ProtocolError(f"input '{name}' appears twice")
        if len(shape) == 1:
            data = data.reshape(-1, 1)
        elif len(shape) != 2:
            raise ProtocolError(
                f"input '{name}': a column must have shape [N] or [N, k], got {shape}"
            )
        by_name[name] = data
    if signature is not None:
        missing = [n for n in signature.names if n not in by_name]
        unknown = [n for n in by_name if n not in signature.names]
        if missing or unknown:
            parts = []
            if missing:
                parts.append(f"missing {missing}")
            if unknown:
                parts.append(f"unknown {unknown}")
            raise ProtocolError(
                f"inputs do not match the model signature ({'; '.join(parts)}); "
                f"expected {list(signature.names)}"
            )
        order = list(signature.names)
    else:
        order = list(by_name)
    rows = {by_name[n].shape[0] for n in order}
    if len(rows) != 1:
        raise ProtocolError(f"inputs disagree on the number of rows: {sorted(rows)}")
    return np.hstack([by_name[n] for n in order])


def _datatype(array: np.ndarray) -> str:
    if array.dtype == np.bool_:
        return "BOOL"
    if np.issubdtype(array.dtype, np.integer):
        return "INT64"
    if np.issubdtype(array.dtype, np.floating):
        return "FP64"
    return "BYTES"


def response(
    model_name: str,
    version: str | None,
    raw: Any,
    *,
    request_id: str | None = None,
    output_name: str = "predict",
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An OIP v2 inference response carrying ``raw`` (the model's prediction) as one tensor."""
    array = np.asarray(raw.to_numpy() if hasattr(raw, "to_numpy") else raw)
    if array.ndim == 0:
        array = array.reshape(1)
    datatype = _datatype(array)
    data = array.ravel().tolist() if datatype != "BYTES" else [str(v) for v in array.ravel()]
    body: dict[str, Any] = {
        "model_name": model_name,
        "model_version": version,
        "outputs": [
            {"name": output_name, "datatype": datatype, "shape": list(array.shape), "data": data}
        ],
    }
    if request_id is not None:
        body["id"] = request_id
    if parameters:
        body["parameters"] = parameters
    return body


def model_metadata(
    model_name: str, versions: list[str], platform: str, signature: Signature | None
) -> dict[str, Any]:
    """``GET /v2/models/{name}``: what a client needs to build a request."""
    if signature is None:
        inputs = [{"name": "input-0", "datatype": "FP64", "shape": [-1, -1]}]
    else:
        inputs = [
            {"name": n, "datatype": t, "shape": [-1]}
            for n, t in zip(signature.names, signature.datatypes, strict=True)
        ]
    return {
        "name": model_name,
        "versions": versions,
        "platform": platform,
        "inputs": inputs,
        "outputs": [{"name": "predict", "datatype": "FP64", "shape": [-1]}],
    }


def infer_parameters(body: Any) -> dict[str, Any]:
    """The request's ``parameters`` object (the platform reads ``alias`` from it), or ``{}``."""
    params = body.get("parameters") if isinstance(body, dict) else None
    return params if isinstance(params, dict) else {}
