"""A6 — Croissant dataset metadata & structured model cards (ADR 0037).

GWT/acceptance from ``design/vision/specs/A6-croissant-model-cards.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def test_r1_croissant_record_has_fdata_fields():
    """R1: the Croissant record maps the real FData columns."""
    from examlops.cards import croissant_record

    rec = croissant_record("FData", revision="abc123")
    field_names = {f["name"] for f in rec["recordSet"][0]["field"]}
    assert field_names == {"pclass", "mbwidth", "embedding"}
    assert rec["version"] == "abc123"
    assert rec["license"]


def test_r2_croissant_validates():
    from examlops.cards import croissant_record, validate_croissant

    rec = croissant_record("FData")
    assert validate_croissant(rec) == []


def test_r2_croissant_validation_catches_errors():
    from examlops.cards import validate_croissant

    bad = {"@context": "wrong", "@type": "sc:Dataset"}
    errors = validate_croissant(bad)
    assert any("@context" in e for e in errors)
    assert any("license" in e for e in errors)


def test_r3_model_card_autopopulates_from_platform():
    """R3: a model card is auto-populated from platform data (D1/C8/A2)."""
    from examlops.cards import build_model_card
    from examlops.compliance import classify_system

    classify_system("JPCP", "high", "HPC job triage", "internal", "tester")
    card = build_model_card("JPCP")
    assert card.fields["intended_use"] == "HPC job triage"
    assert card.fields["risk_class"] == "high"


def test_r4_missing_fields_are_not_provided_not_fabricated():
    """R4: missing fields render as 'not provided', never fabricated."""
    from examlops.cards import NOT_PROVIDED, build_model_card

    card = build_model_card("Unknown")
    assert card.fields["fairness"] == NOT_PROVIDED
    assert card.fields["metrics"] == NOT_PROVIDED
    assert NOT_PROVIDED in card.to_markdown()


def test_r6_completeness_scoring():
    """R6: completeness reflects how many fields are populated."""
    from examlops.cards import build_model_card, card_completeness
    from examlops.compliance import classify_system

    empty = card_completeness("Nothing")
    assert empty == 0.0

    classify_system("JPCP", "high", "purpose", "ctx", "tester")
    card = build_model_card("JPCP")
    assert card.completeness > 0.0  # intended_use + risk_class populated
    assert card.completeness < 1.0  # fairness/lineage/metrics still missing


def test_card_populates_fairness_and_lineage():
    from examlops import platform_db
    from examlops.cards import NOT_PROVIDED, build_model_card
    from examlops.compliance import classify_system

    classify_system("JPCP", "high", "p", "c", "tester")
    # C8 fairness
    platform_db.set_fairness_config("JPCP", ["region"], min_samples=2)
    platform_db.record_fairness_sample("JPCP", "region", "north", prediction=1.0, label=1.0)
    platform_db.record_fairness_sample("JPCP", "region", "north", prediction=1.0, label=1.0)
    # A2 lineage
    with platform_db.get_db() as conn:
        conn.execute(
            "INSERT INTO lineage_events (run_id, job, event_type, model) VALUES (?,?,?,?)",
            ("r1", "train", "COMPLETE", "JPCP"),
        )
    card = build_model_card("JPCP")
    assert card.fields["fairness"] != NOT_PROVIDED
    assert card.fields["lineage"] != NOT_PROVIDED


def test_card_versioned_and_persisted():
    from examlops import platform_db
    from examlops.cards import build_model_card

    card = build_model_card("JPCP")
    v1 = platform_db.save_model_card("JPCP", "{}", card.completeness)
    v2 = platform_db.save_model_card("JPCP", "{}", card.completeness)
    assert v1 == 1 and v2 == 2
    assert platform_db.get_model_card("JPCP")["version"] == 2


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.compliance import classify_system

    classify_system("JPCP", "high", "triage", "ctx", "tester")
    runner = CliRunner()
    r1 = runner.invoke(app, ["cards", "dataset", "FData"])
    assert r1.exit_code == 0, r1.output
    assert "pclass" in r1.output
    r2 = runner.invoke(app, ["cards", "model", "JPCP"])
    assert r2.exit_code == 0, r2.output
    assert "Model Card" in r2.output
    r3 = runner.invoke(app, ["cards", "completeness", "JPCP"])
    assert r3.exit_code == 0, r3.output


def test_cli_completeness_gate_exit():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    # Unpopulated model, require 50% => exit 1.
    result = runner.invoke(app, ["cards", "completeness", "Nothing", "--require", "0.5"])
    assert result.exit_code == 1
