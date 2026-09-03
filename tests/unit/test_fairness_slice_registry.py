# tests/unit/test_fairness_slice_registry.py
"""ADR 0025 clause 1 — the slice registry declared per model in the model YAML.

The clause puts the registry "per model in the model YAML (categorical/binned features;
protected attributes for people-facing models)". Everything downstream shipped — `slice_metrics`,
the disparity computation, the promotion gate — but the attributes they all slice on lived only
in a runtime `fairness_config` row: not in code review, not in the deployment, and gone when the
database is rebuilt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

from examlops import fairness  # noqa: E402
from examlops.cli.commands import fairness_cmd  # noqa: E402
from examlops.data.governance import set_fairness_config  # noqa: E402
from examlops.fairness import (  # noqa: E402
    effective_fairness_config,
    fairness_config_drift,
    validate_fairness_block,
)

runner = CliRunner()

_BLOCK = {"slices": ["region", "tier"], "threshold": 0.2, "min_samples": 5, "gate_promotion": True}


@pytest.fixture
def declared(tmp_path, monkeypatch):
    """A pack whose model YAML declares a slice registry."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "widget.yaml").write_text(
        yaml.safe_dump(
            {"name": "Widget", "config_class": "c", "task_type": "regression", "fairness": _BLOCK}
        )
    )
    monkeypatch.setattr("examlops.usecase.models_dir", lambda *a, **k: models)
    return "Widget"


# ── validation: a typo must fail CI, not disable the gate ─────────────────────


def test_a_valid_block_passes():
    assert validate_fairness_block(_BLOCK) == []


def test_no_block_is_not_an_error():
    assert validate_fairness_block(None) == []
    assert validate_fairness_block({}) == []


def test_an_unknown_key_is_an_error_not_ignored():
    """`slice:` for `slices:` yields a registry of zero attributes, and a gate over zero
    attributes passes every model — a protection that silently does not apply."""
    errors = validate_fairness_block({"slice": ["region"]})
    assert any("unknown fairness key 'slice'" in e for e in errors)
    assert any("must declare 'slices'" in e for e in errors)


def test_an_empty_slice_list_is_rejected():
    assert "empty" in " ".join(validate_fairness_block({"slices": []}))


def test_slices_must_be_attribute_names():
    assert validate_fairness_block({"slices": [1, 2]}) != []
    assert validate_fairness_block({"slices": "region"}) != []


def test_an_integer_threshold_is_accepted():
    """YAML parses `threshold: 1` as an int; a float-only check would reject a valid value."""
    assert validate_fairness_block({"slices": ["a"], "threshold": 1}) == []
    assert validate_fairness_block({"slices": ["a"], "threshold": 0}) == []


def test_an_out_of_range_threshold_is_rejected():
    assert "between 0 and 1" in " ".join(validate_fairness_block({"slices": ["a"], "threshold": 5}))


def test_a_boolean_is_not_a_number():
    """bool subclasses int, so a naive isinstance check would accept `min_samples: true`."""
    assert validate_fairness_block({"slices": ["a"], "min_samples": True}) != []
    assert validate_fairness_block({"slices": ["a"], "threshold": True}) != []


def test_the_error_names_the_model_when_given_one():
    assert "Widget: " in validate_fairness_block({"slices": []}, model="Widget")[0]


# ── the loader must carry the block ───────────────────────────────────────────


def test_the_block_reaches_the_parsed_config(tmp_path):
    """The `engine:` key was silently dropped by this loader once; that is the trap here."""
    from pipelines.model_loader import load_model_yaml

    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {"name": "M", "config_class": "c", "task_type": "regression", "fairness": _BLOCK}
        )
    )
    assert load_model_yaml(path).fairness == _BLOCK


def test_a_model_without_the_block_gets_an_empty_mapping(tmp_path):
    from pipelines.model_loader import load_model_yaml

    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump({"name": "M", "config_class": "c", "task_type": "regression"}))
    assert load_model_yaml(path).fairness == {}


# ── the declaration is in force without an apply step ─────────────────────────


def test_a_declared_registry_is_effective_with_no_runtime_row(declared):
    """A registry that only counts once someone materialises it is a protection that
    silently does not exist."""
    cfg, source = effective_fairness_config(declared)
    assert source == "yaml"
    assert cfg["slice_attrs"] == ["region", "tier"]
    assert cfg["threshold"] == 0.2
    assert cfg["gate_promotion"] is True


def test_the_promotion_gate_honours_a_yaml_only_declaration(declared, monkeypatch):
    monkeypatch.setattr(
        fairness,
        "slice_metrics",
        lambda model, attr, **kw: type("R", (), {"disparity_exceeded": attr == "tier"})(),
    )
    assert fairness.fairness_gate(declared) is True


def test_a_model_with_neither_source_has_no_registry():
    cfg, source = effective_fairness_config("Nothing")
    assert cfg is None and source == "none"


def test_an_invalid_block_is_not_silently_treated_as_a_registry(tmp_path, monkeypatch):
    """A block that fails validation must not half-apply — it declares nothing."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "bad.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "Bad",
                "config_class": "c",
                "task_type": "regression",
                "fairness": {"slice": ["region"]},
            }
        )
    )
    monkeypatch.setattr("examlops.usecase.models_dir", lambda *a, **k: models)
    assert effective_fairness_config("Bad") == (None, "none")


# ── precedence and drift ──────────────────────────────────────────────────────


def test_a_runtime_row_wins_over_the_declaration(declared):
    """Writing one is a deliberate act on a live system; the YAML overriding it would make a
    shipped write surface look broken."""
    set_fairness_config(declared, ["country"], threshold=0.05)
    cfg, source = effective_fairness_config(declared)
    assert source == "db"
    assert cfg["slice_attrs"] == ["country"]


def test_disagreement_is_reported_not_resolved_silently(declared):
    set_fairness_config(declared, ["country"], threshold=0.05)
    drift = fairness_config_drift(declared)
    assert any("slice_attrs" in d for d in drift)
    assert any("threshold" in d for d in drift)


def test_no_drift_is_reported_when_they_agree(declared):
    set_fairness_config(
        declared, ["region", "tier"], threshold=0.2, min_samples=5, gate_promotion=True
    )
    assert fairness_config_drift(declared) == []


def test_drift_needs_both_sides():
    set_fairness_config("DbOnly", ["a"])
    assert fairness_config_drift("DbOnly") == []


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_show_reports_the_source(declared, monkeypatch):
    from examlops.cli import _output

    monkeypatch.setattr(_output, "json_mode", True)
    result = runner.invoke(fairness_cmd.app, ["show", declared])
    assert result.exit_code == 0
    assert '"source": "yaml"' in result.output


def test_show_says_plainly_when_no_registry_exists(monkeypatch):
    result = runner.invoke(fairness_cmd.app, ["show", "Nothing"])
    assert result.exit_code == 0
    assert "Fairness gating does not apply" in result.output


def test_apply_materialises_the_declaration_and_audits_it(declared):
    from examlops.data.audit import export_audit_events

    assert runner.invoke(fairness_cmd.app, ["apply", declared]).exit_code == 0
    cfg, source = effective_fairness_config(declared)
    assert source == "db"
    assert cfg["slice_attrs"] == ["region", "tier"]
    assert [e for e in export_audit_events() if e["action"] == "fairness_config_applied"]


def test_apply_refuses_a_model_with_no_declaration():
    result = runner.invoke(fairness_cmd.app, ["apply", "Nothing"])
    assert result.exit_code == 1
    assert "no valid `fairness:` block" in result.output
