"""ADR 0017 clause 2 — one typed feature definition, one transform for train and serve."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.feature_store.spec import (  # noqa: E402
    FeatureDefinitionError,
    FeatureValidationError,
    ViewDefinition,
    transform_row,
)

_BASE = {
    "name": "jobs",
    "entity": "job",
    "entity_key": "job_id",
    "timestamp_field": "adt",
    "embedding_feature": "embedding",
    "features": [
        {"name": "embedding", "dtype": "vector", "dim": 3},
        {"name": "mbwidth", "dtype": "float", "required": False},
        {"name": "nodes", "dtype": "int"},
        {"name": "pclass", "dtype": "str", "required": False},
    ],
}


def _view(**over):
    return ViewDefinition.from_dict({**_BASE, **over})


def test_transform_keeps_declared_features_in_order_and_coerces():
    out = transform_row(
        _view(), {"embedding": [1, 2, 3], "nodes": "4", "extra": "dropped", "mbwidth": "2.5"}
    )
    assert out == {"embedding": [1.0, 2.0, 3.0], "mbwidth": 2.5, "nodes": 4, "pclass": None}
    assert list(out) == ["embedding", "mbwidth", "nodes", "pclass"]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"nodes": 1}, "embedding is required"),
        ({"embedding": [1, 2], "nodes": 1}, "expected 3 dims, got 2"),
        ({"embedding": "abc", "nodes": 1}, "must be a list of numbers"),
        ({"embedding": [1, "x", 3], "nodes": 1}, "must be a list of numbers"),
        ({"embedding": [1, math.inf, 3], "nodes": 1}, "non-finite"),
        ({"embedding": [1, 2, 3], "nodes": 1.5}, "nodes must be an integer"),
        ({"embedding": [1, 2, 3], "nodes": True}, "nodes must be an integer"),
        ({"embedding": [1, 2, 3], "nodes": 1, "mbwidth": math.nan}, None),
        ({"embedding": [1, 2, 3], "nodes": 1, "mbwidth": "fast"}, "mbwidth must be a number"),
        ({"embedding": [1, 2, 3]}, "nodes is required"),
    ],
)
def test_transform_refuses_what_the_definition_does_not_allow(row, message):
    if message is None:  # NaN in an optional feature reads as missing, not as a value
        assert transform_row(_view(), row)["mbwidth"] is None
        return
    with pytest.raises(FeatureValidationError, match=message):
        transform_row(_view(), row)


def test_validation_error_is_a_value_error_so_serving_maps_it_to_validation_error():
    assert issubclass(FeatureValidationError, ValueError)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"name": ""}, "'name' and 'entity' are required"),
        ({"features": []}, "non-empty list"),
        ({"features": [{"name": "e", "dtype": "vector"}]}, "integer dim"),
        ({"features": [{"name": "e", "dtype": "vector", "dim": 10**9}]}, "integer dim"),
        ({"features": [{"name": "e", "dtype": "float", "dim": 3}]}, "only a vector takes a dim"),
        ({"features": [{"name": "e", "dtype": "tensor"}]}, "dtype 'tensor'"),
        ({"features": ["a", "a"]}, "duplicate feature names"),
        ({"embedding_feature": "ghost"}, "not a declared feature"),
        ({"embedding_feature": "nodes"}, "must be a vector feature"),
        ({"ttl_seconds": -1}, "ttl_seconds must be a non-negative integer"),
        ({"materialize_interval_seconds": "hourly"}, "materialize_interval_seconds"),
    ],
)
def test_malformed_definitions_are_refused(over, message):
    with pytest.raises(FeatureDefinitionError, match=message):
        _view(**over)


def test_fingerprint_covers_the_contract_not_the_operational_knobs():
    base = _view()
    assert (
        base.fingerprint() == _view(ttl_seconds=10, materialize_interval_seconds=60).fingerprint()
    )
    assert base.fingerprint() == _view(serving=True, description="x").fingerprint()
    changed = dict(_BASE["features"][0], dim=4)
    other = _view(features=[changed, *_BASE["features"][1:]])
    assert other.fingerprint() != base.fingerprint()
    assert base.spec_dict()["fingerprint"] == base.fingerprint()
