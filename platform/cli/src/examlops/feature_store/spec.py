"""The single transform definition of a feature view (ADR 0017 clause 2).

A :class:`ViewDefinition` is the one place a feature's shape is written down — its type, its
dimension, whether it is required. :func:`transform_row` is the one function that turns a raw
row (a training-set row *or* an inference request) into the model's feature dict under that
definition. Training's feature gate and serving's ``FeatureTransformer`` both call it, so there is
no second copy of the transform that can drift: change the definition and both sides change.

Deliberately dependency-free (stdlib only): serving imports it on the request path and the
pipeline engine imports it on the orchestrator, neither of which should pull a data stack for it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

Dtype = Literal["vector", "float", "int", "str", "bool"]
_DTYPES: tuple[str, ...] = ("vector", "float", "int", "str", "bool")

#: Hard ceiling on a declared vector dimension — a typo like ``38400`` must not let one request
#: allocate a huge list on the serving path.
MAX_VECTOR_DIM = 65_536


class FeatureDefinitionError(ValueError):
    """A view definition is malformed (bad dtype, missing dim, duplicate feature, …)."""


class FeatureValidationError(ValueError):
    """A row does not satisfy its view definition. Subclasses ``ValueError`` on purpose:
    serving already maps ``ValueError`` from the transform to a ``validation_error`` reply."""


@dataclass(frozen=True)
class FeatureSpec:
    """One declared feature: its name, type and (for a vector) its exact dimension."""

    name: str
    dtype: Dtype = "float"
    dim: int | None = None
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "dtype": self.dtype, "required": self.required}
        if self.dim is not None:
            d["dim"] = self.dim
        return d

    @classmethod
    def from_dict(cls, raw: Any) -> FeatureSpec:
        if isinstance(raw, str):  # shorthand: a bare name is an untyped (float) feature
            raw = {"name": raw}
        if not isinstance(raw, dict) or not raw.get("name"):
            raise FeatureDefinitionError(f"feature entry needs a 'name': {raw!r}")
        dtype = str(raw.get("dtype", "float"))
        if dtype not in _DTYPES:
            raise FeatureDefinitionError(
                f"feature '{raw['name']}': dtype {dtype!r} is not one of {list(_DTYPES)}"
            )
        dim = raw.get("dim")
        if dtype == "vector":
            if not isinstance(dim, int) or isinstance(dim, bool) or not 0 < dim <= MAX_VECTOR_DIM:
                raise FeatureDefinitionError(
                    f"vector feature '{raw['name']}' needs an integer dim in 1..{MAX_VECTOR_DIM}"
                )
        elif dim is not None:
            raise FeatureDefinitionError(f"feature '{raw['name']}': only a vector takes a dim")
        return cls(
            name=str(raw["name"]),
            dtype=dtype,  # type: ignore[arg-type]
            dim=dim,
            required=bool(raw.get("required", True)),
        )


@dataclass(frozen=True)
class ViewDefinition:
    """A feature view as written in a use-case pack's ``features/*.yaml``.

    ``features`` + ``entity`` + ``embedding_feature`` are the *contract* (they enter the
    fingerprint). ``ttl_seconds``, ``materialize_interval_seconds`` and ``serving`` are
    operational knobs: changing them must not make a trained model look skewed.
    """

    name: str
    entity: str
    features: tuple[FeatureSpec, ...]
    entity_key: str | None = None
    timestamp_field: str | None = None
    #: The training-data column naming the entity, when it differs from the request field
    #: ``entity_key`` (FData calls it ``jid``, requests call it ``job_id``). ``None`` = same name.
    entity_column: str | None = None
    source: str | None = None
    ttl_seconds: int = 0
    materialize_interval_seconds: int = 0
    embedding_feature: str | None = None
    serving: bool = False
    description: str = ""
    origin: str = field(default="", compare=False)

    @property
    def dataset_entity_column(self) -> str | None:
        """The training-data column holding the entity id (``entity_column`` or ``entity_key``)."""
        return self.entity_column or self.entity_key

    @property
    def feature_names(self) -> list[str]:
        return [f.name for f in self.features]

    def fingerprint(self) -> str:
        """sha256 over the contract fields only — equal fingerprints mean one definition."""
        contract = {
            "name": self.name,
            "entity": self.entity,
            "features": [f.to_dict() for f in self.features],
            "embedding_feature": self.embedding_feature,
        }
        blob = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def spec_dict(self) -> dict[str, Any]:
        """What is persisted alongside the registry row (``feature_views.spec_json``)."""
        return {
            "features": [f.to_dict() for f in self.features],
            "entity_key": self.entity_key,
            "entity_column": self.entity_column,
            "timestamp_field": self.timestamp_field,
            "serving": self.serving,
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, raw: Any, *, origin: str = "") -> ViewDefinition:
        if not isinstance(raw, dict):
            raise FeatureDefinitionError(f"{origin or 'view'}: a view definition is a mapping")
        name = raw.get("name")
        entity = raw.get("entity")
        if not name or not entity:
            raise FeatureDefinitionError(f"{origin or 'view'}: 'name' and 'entity' are required")
        feats_raw = raw.get("features") or []
        if not isinstance(feats_raw, list) or not feats_raw:
            raise FeatureDefinitionError(f"view '{name}': 'features' must be a non-empty list")
        specs = tuple(FeatureSpec.from_dict(f) for f in feats_raw)
        names = [s.name for s in specs]
        if len(set(names)) != len(names):
            raise FeatureDefinitionError(f"view '{name}': duplicate feature names {names}")
        emb = raw.get("embedding_feature") or None
        if emb is not None:
            match = [s for s in specs if s.name == emb]
            if not match:
                raise FeatureDefinitionError(
                    f"view '{name}': embedding_feature '{emb}' is not a declared feature"
                )
            if match[0].dtype != "vector":
                raise FeatureDefinitionError(
                    f"view '{name}': embedding_feature '{emb}' must be a vector feature"
                )
        ttl = _non_negative_int(raw.get("ttl_seconds", 0), f"view '{name}': ttl_seconds")
        interval = _non_negative_int(
            raw.get("materialize_interval_seconds", 0),
            f"view '{name}': materialize_interval_seconds",
        )
        return cls(
            name=str(name),
            entity=str(entity),
            features=specs,
            entity_key=raw.get("entity_key") or None,
            entity_column=raw.get("entity_column") or None,
            timestamp_field=raw.get("timestamp_field") or None,
            source=raw.get("source") or None,
            ttl_seconds=ttl,
            materialize_interval_seconds=interval,
            embedding_feature=emb,
            serving=bool(raw.get("serving", False)),
            description=str(raw.get("description") or ""),
            origin=origin,
        )


def _non_negative_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FeatureDefinitionError(f"{what} must be a non-negative integer, got {value!r}")
    return value


def _coerce(spec: FeatureSpec, value: Any) -> Any:
    if spec.dtype == "vector":
        if isinstance(value, (str, bytes)) or not hasattr(value, "__len__"):
            raise FeatureValidationError(f"{spec.name} must be a list of numbers")
        if len(value) != spec.dim:
            raise FeatureValidationError(f"expected {spec.dim} dims, got {len(value)}")
        try:
            vec = [float(x) for x in value]
        except (TypeError, ValueError) as exc:
            raise FeatureValidationError(f"{spec.name} must be a list of numbers") from exc
        if not all(math.isfinite(x) for x in vec):
            raise FeatureValidationError(f"{spec.name} contains a non-finite value")
        return vec
    if spec.dtype == "float":
        try:
            f = float(value)
        except (TypeError, ValueError) as exc:
            raise FeatureValidationError(f"{spec.name} must be a number") from exc
        if not math.isfinite(f):
            raise FeatureValidationError(f"{spec.name} must be finite")
        return f
    if spec.dtype == "int":
        if isinstance(value, bool):
            raise FeatureValidationError(f"{spec.name} must be an integer")
        try:
            i = int(value)
        except (TypeError, ValueError) as exc:
            raise FeatureValidationError(f"{spec.name} must be an integer") from exc
        if isinstance(value, float) and value != i:
            raise FeatureValidationError(f"{spec.name} must be an integer")
        return i
    if spec.dtype == "bool":
        if not isinstance(value, bool):
            raise FeatureValidationError(f"{spec.name} must be a boolean")
        return value
    return str(value)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, float) and math.isnan(value)


def transform_row(view: ViewDefinition, row: dict[str, Any]) -> dict[str, Any]:
    """The model's feature dict for ``row`` under ``view`` — the one train/serve transform.

    Keeps exactly the declared features, in declared order; coerces each to its dtype; refuses a
    missing required feature (``"<name> is required"``) or a wrong-shaped one. An optional feature
    that is missing comes through as ``None`` so the dict's keys never depend on the input.
    """
    out: dict[str, Any] = {}
    for spec in view.features:
        value = row.get(spec.name)
        if _is_missing(value):
            if spec.required:
                raise FeatureValidationError(f"{spec.name} is required")
            out[spec.name] = None
            continue
        out[spec.name] = _coerce(spec, value)
    return out


__all__ = [
    "MAX_VECTOR_DIM",
    "FeatureDefinitionError",
    "FeatureSpec",
    "FeatureValidationError",
    "ViewDefinition",
    "transform_row",
]
