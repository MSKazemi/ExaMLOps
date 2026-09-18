"""Unit tests for examlops.dataplane.streams.schema (ADR 0130/0131, Plan 2, task A2).

The ``Test*`` classes below port every test from ``tests/unit/test_model_schema_registry.py``
verbatim (same fixtures, same assertions) against the new package, proving the port is faithful:
same error messages, same "no input_schema -> skip" rule, same "None counts as missing" rule. The
rest of the file covers the new payload-side helpers (``validate``/``build_body``) added for the
stream ingress: passthrough forwarding, NaN/Inf rejection, oversize rejection, and an extra
skip-shape case.
"""

from __future__ import annotations

import math
import textwrap
from unittest.mock import MagicMock

import pytest

from examlops.dataplane.streams.schema import (
    ModelSchemaRegistry,
    build_body,
    reset_default_registry,
    validate,
)
from examlops.dataplane.streams.types import StreamBinding, StreamLimits
from examlops.dataplane.types import SpecError

JPCP_YAML = textwrap.dedent("""\
    name: JPCP
    task_type: regression
    inference:
      input_schema:
        embedding: list[float]
      output_schema:
        power_per_node_watts: float
""")

MACK_YAML = textwrap.dedent("""\
    name: MACK
    task_type: classification
    inference:
      input_schema:
        embedding: list[float]
      output_schema:
        pclass: int
""")


@pytest.fixture
def yaml_dir(tmp_path):
    (tmp_path / "jpcp.yaml").write_text(JPCP_YAML)
    (tmp_path / "mack.yaml").write_text(MACK_YAML)
    return tmp_path


# --- ported verbatim from tests/unit/test_model_schema_registry.py --------------------------


def test_loads_all_models(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    assert set(reg.models()) == {"JPCP", "MACK"}


def test_models_returns_uppercase(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    assert "jpcp" not in reg.models()


def test_build_features_extracts_embedding(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    msg = MagicMock()
    msg.embedding = [0.1] * 384
    features = reg.build_features("JPCP", msg)
    assert "embedding" in features
    assert len(features["embedding"]) == 384


def test_build_features_unknown_model_falls_back(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    msg = MagicMock()
    msg.embedding = [0.5] * 384
    features = reg.build_features("UNKNOWN", msg)
    assert "embedding" in features


def test_validate_features_passes_correct_list(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    assert reg.validate_features("JPCP", {"embedding": [0.1] * 384}) is None


def test_validate_features_raises_on_missing_field(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    with pytest.raises(ValueError, match="Missing required feature"):
        reg.validate_features("JPCP", {})


def test_validate_features_raises_on_wrong_type(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    with pytest.raises(ValueError, match="must be a list"):
        reg.validate_features("JPCP", {"embedding": 42.0})


def test_skips_yaml_without_input_schema(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: BROKEN\ntask_type: regression\n")
    reg = ModelSchemaRegistry(tmp_path)
    assert "BROKEN" not in reg.models()


def test_build_features_raises_on_missing_msg_attribute(yaml_dir):
    reg = ModelSchemaRegistry(yaml_dir)
    msg = object()  # no 'embedding' attribute
    with pytest.raises(ValueError, match="has no attribute"):
        reg.build_features("JPCP", msg)


# --- new: models_dir() seam instead of a hardcoded pipelines/models path --------------------


def test_registry_defaults_to_models_dir(monkeypatch, yaml_dir):
    """With no explicit yaml_dir, the registry follows examlops.usecase.models_dir()."""
    monkeypatch.setenv("RAY_MODELS_DIR", str(yaml_dir))
    reg = ModelSchemaRegistry()
    assert set(reg.models()) == {"JPCP", "MACK"}


# --- new: an additional skip-shape case (empty, not absent, input_schema) -------------------


def test_skips_yaml_with_empty_input_schema_mapping(tmp_path):
    (tmp_path / "empty.yaml").write_text(
        "name: EMPTY\ntask_type: regression\ninference:\n  input_schema: {}\n"
    )
    reg = ModelSchemaRegistry(tmp_path)
    assert "EMPTY" not in reg.models()


# --- new: validate() — NaN/Inf and oversize rejection ----------------------------------------


def test_validate_accepts_a_clean_payload():
    payload = {"embedding": [0.1, 0.2, 0.3], "num_nodes": 4}
    assert validate(payload, max_bytes=1_048_576) is None
    assert payload == {"embedding": [0.1, 0.2, 0.3], "num_nodes": 4}  # left untouched


def test_validate_rejects_nan_in_a_scalar():
    with pytest.raises(SpecError, match="not finite"):
        validate({"score": float("nan")}, max_bytes=1_048_576)


def test_validate_rejects_inf_inside_a_vector():
    with pytest.raises(SpecError, match=r"payload\.embedding\[2\] is not finite"):
        validate({"embedding": [0.1, 0.2, math.inf]}, max_bytes=1_048_576)


def test_validate_rejects_nan_nested_in_a_dict():
    with pytest.raises(SpecError, match=r"payload\.metrics\.rmse is not finite"):
        validate({"metrics": {"rmse": float("nan")}}, max_bytes=1_048_576)


def test_validate_rejects_oversize_payload():
    with pytest.raises(SpecError, match="exceeds the 10-byte limit"):
        validate({"embedding": [0.1] * 100}, max_bytes=10)


def test_validate_rejects_vector_longer_than_max_vector_len():
    with pytest.raises(SpecError, match="exceeding the 4-element limit"):
        validate({"embedding": [0.1, 0.2, 0.3, 0.4, 0.5]}, max_bytes=1_048_576, max_vector_len=4)


def test_validate_allows_a_bool_value():
    """bool is a subclass of int/float-adjacent in Python; it must never trip the finite check."""
    assert validate({"flag": True}, max_bytes=1_048_576) is None


def test_validate_rejects_payload_nested_deeper_than_max_depth():
    payload = {"v": 0.0}
    for _ in range(40):
        payload = {"child": payload}
    with pytest.raises(SpecError, match="nested deeper than 32 levels"):
        validate(payload, max_bytes=10_000_000)


# --- new: build_body() — schema fields + options.passthrough ---------------------------------


@pytest.fixture
def _schema_env(monkeypatch, yaml_dir):
    """Point the default registry at *yaml_dir* for the duration of one test."""
    monkeypatch.setenv("RAY_MODELS_DIR", str(yaml_dir))
    reset_default_registry()
    yield
    reset_default_registry()


def _binding(**overrides) -> StreamBinding:
    fields = dict(
        project="default",
        name="jpcp-stream",
        connector="kafka",
        model="JPCP",
        alias="production",
        address="topic://jpcp.requests",
        connection=None,
        options={},
        limits=StreamLimits(),
    )
    fields.update(overrides)
    return StreamBinding(**fields)


def test_build_body_forwards_schema_fields_only(_schema_env):
    binding = _binding()
    body = build_body(binding, {"embedding": [0.1, 0.2], "junk": "drop-me"})
    assert body == {"embedding": [0.1, 0.2]}


def test_build_body_forwards_passthrough_keys(_schema_env):
    """num_nodes lives outside JPCP's input schema but must still reach the model service."""
    binding = _binding(options={"passthrough": ["num_nodes"]})
    body = build_body(binding, {"embedding": [0.1, 0.2], "num_nodes": 8})
    assert body == {"embedding": [0.1, 0.2], "num_nodes": 8}


def test_build_body_missing_passthrough_key_is_not_an_error(_schema_env):
    binding = _binding(options={"passthrough": ["num_nodes"]})
    body = build_body(binding, {"embedding": [0.1, 0.2]})
    assert body == {"embedding": [0.1, 0.2]}


def test_build_body_raises_on_missing_schema_field(_schema_env):
    binding = _binding()
    with pytest.raises(SpecError, match="Missing required feature 'embedding'"):
        build_body(binding, {})


def test_build_body_treats_none_value_as_missing(_schema_env):
    binding = _binding()
    with pytest.raises(SpecError, match="Missing required feature 'embedding'"):
        build_body(binding, {"embedding": None})


def test_build_body_unknown_model_forwards_payload_unchanged(_schema_env):
    binding = _binding(model="UNKNOWN")
    body = build_body(binding, {"whatever": 1})
    assert body == {"whatever": 1}


def test_build_body_rejects_non_list_passthrough(_schema_env):
    """A string passthrough value must fail loudly, not iterate character by character."""
    binding = _binding(options={"passthrough": "num_nodes"})
    with pytest.raises(SpecError, match="must be a list of strings"):
        build_body(binding, {"embedding": [0.1, 0.2], "num_nodes": 8})


def test_build_body_rejects_non_string_passthrough_items(_schema_env):
    binding = _binding(options={"passthrough": [1, 2]})
    with pytest.raises(SpecError, match="must be a list of strings"):
        build_body(binding, {"embedding": [0.1, 0.2]})


def test_build_body_injected_registry_wins_over_default(monkeypatch, yaml_dir, tmp_path):
    """An explicit registry must win over whatever the default registry resolved to."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("RAY_MODELS_DIR", str(empty_dir))
    reset_default_registry()
    try:
        binding = _binding()
        payload = {"embedding": [0.1, 0.2], "junk": "drop-me"}

        # No injected registry: the default (pointed at an empty dir) has no schema for JPCP,
        # so the payload is forwarded unchanged, junk included.
        assert build_body(binding, payload) == payload

        # An injected registry that DOES know JPCP's schema wins and drops "junk".
        injected = ModelSchemaRegistry(yaml_dir)
        body = build_body(binding, payload, registry=injected)
        assert body == {"embedding": [0.1, 0.2]}
    finally:
        reset_default_registry()
