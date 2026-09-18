"""Calling the model server over Open Inference Protocol v2 from platform code (plan P4.5, ADR 0126).

The platform's own callers — the inference router, the bus bridge, the agent's predict tool, the
dashboard's test inference, ``exa batch`` — held a feature dict (or a bare vector) and read one
``prediction`` back from the deprecated ``POST /predict/{model}``. These two functions let them
speak OIP v2 without each learning the protocol:

* :func:`features_request` turns the features into OIP v2 input tensors, one per feature, named
  as the feature. A model with a column signature gets its columns by name (the server matches
  them); a model without one gets them in the given order, as ``/predict`` flattened them.
* :func:`result` turns the OIP v2 answer back into the shape those callers already read:
  ``model_name``, ``model_version``, ``alias``, ``run_id`` and ``prediction`` (a scalar for one
  value, a list otherwise).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote


def infer_path(model: str, version: str | None = None) -> str:
    """``/v2/models/{model}[/versions/{version}]/infer``, segments escaped."""
    name = quote(str(model), safe="")
    if version:
        return f"/v2/models/{name}/versions/{quote(str(version), safe='')}/infer"
    return f"/v2/models/{name}/infer"


def _tensor(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, (list, tuple)):
        data = list(value)
        return {"name": name, "shape": [1, len(data)], "datatype": "FP64", "data": data}
    return {"name": name, "shape": [1], "datatype": "FP64", "data": [value]}


def features_request(
    features: Mapping[str, Any] | Sequence[Any],
    *,
    alias: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """An OIP v2 inference request for one row of ``features``.

    A mapping becomes one tensor per feature; a bare vector becomes one ``[1, F]`` tensor.
    """
    if isinstance(features, Mapping):
        inputs = [_tensor(str(name), value) for name, value in features.items()]
    else:
        inputs = [_tensor("input-0", list(features))]
    body: dict[str, Any] = {"inputs": inputs}
    if alias:
        body["parameters"] = {"alias": alias}
    if request_id:
        body["id"] = request_id
    return body


def result(response: Mapping[str, Any]) -> dict[str, Any]:
    """The legacy ``/predict`` answer, read out of an OIP v2 inference response.

    ``ValueError`` for anything that is not an inference answer: read leniently, one came back as
    ``prediction: []``, a success nobody could tell from a real prediction.
    """
    outputs = response.get("outputs") if isinstance(response, Mapping) else None
    if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], Mapping):
        raise ValueError("not an Open Inference Protocol v2 answer: no outputs")
    if "data" not in outputs[0]:
        raise ValueError("not an Open Inference Protocol v2 answer: outputs[0] has no data")
    data = list(outputs[0]["data"] or [])
    params = response.get("parameters") or {}
    return {
        "model_name": response.get("model_name"),
        "model_version": response.get("model_version"),
        "alias": params.get("alias"),
        "run_id": params.get("run_id"),
        "prediction": data[0] if len(data) == 1 else data,
    }
