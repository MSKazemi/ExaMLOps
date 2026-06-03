"""Unit tests for ModelSchemaRegistry."""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "platform", "clients"))

# PyYAML is a declared project dependency and is always available
import yaml  # noqa: F401 — imported via model_schema_registry

from model_schema_registry import ModelSchemaRegistry  # noqa: E402


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
    reg.validate_features("JPCP", {"embedding": [0.1] * 384})  # must not raise


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
