# tests/unit/test_card_export_scrub.py
"""ADR 0037 clause 4 — publishing a card through the naming-scrub path.

The clause reads "export/publish with the existing naming-scrub path; PII/internal fields
excluded". The cards themselves shipped, the scrub components shipped (D8 `redact_pii`, the
secret scanner), and nothing connected them: there was no way to get a card out of the platform
except by reading it whole, internal fields and all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cards import INTERNAL_FIELDS, export_card  # noqa: E402
from examlops.cli.commands import cards_a6_cmd  # noqa: E402

runner = CliRunner()


def _card(**over):
    base = {"model": "JPCP", "tenant": "acme", "completeness": 0.5, "intended_use": "HPC power"}
    base.update(over)
    return base


# ── internal fields are dropped, not redacted ─────────────────────────────────


def test_the_tenant_is_removed_outright():
    """On a shared platform it names which customer the card belongs to."""
    result = export_card(_card())
    assert "tenant" not in result.content
    assert result.removed_fields == ["tenant"]
    assert "acme" not in str(result.content)


def test_every_declared_internal_field_is_dropped():
    result = export_card({f: "x" for f in INTERNAL_FIELDS} | {"model": "M"})
    assert set(result.removed_fields) == set(INTERNAL_FIELDS)


def test_the_publishable_content_keeps_everything_else():
    result = export_card(_card())
    assert result.content["model"] == "JPCP"
    assert result.content["intended_use"] == "HPC power"


# ── PII and locations are redacted ────────────────────────────────────────────


def test_pii_is_redacted_through_the_d8_path():
    result = export_card(_card(limitations="contact alice@example.com"))
    assert "alice@example.com" not in str(result.content)
    assert any("email" in r for r in result.redactions)


def test_a_filesystem_path_is_redacted():
    """A deployment's filesystem layout is site-specific detail, not a model property."""
    result = export_card(_card(limitations="weights at /nfs/share01/examlops/jpcp"))
    assert "/nfs/share01" not in str(result.content)
    assert any("filesystem-path" in r for r in result.redactions)


@pytest.mark.parametrize(
    "text", ["http://10.0.0.4:8000/infer", "192.168.1.9", "http://localhost:18001"]
)
def test_private_and_loopback_addresses_are_redacted(text):
    result = export_card(_card(limitations=f"served from {text}"))
    assert text.split("//")[-1] not in str(result.content)


def test_a_public_hostname_is_left_alone():
    """Over-redaction makes a card useless; only site-internal detail goes."""
    result = export_card(_card(limitations="see https://example.org/docs"))
    assert "example.org/docs" in str(result.content)


def test_nested_values_are_scrubbed_not_just_the_top_level():
    """`metrics` and `fairness` are nested mappings; a shallow pass would publish them."""
    result = export_card(_card(fairness={"region": {"note": "owner bob@example.com"}}))
    assert "bob@example.com" not in str(result.content)


def test_keys_are_scrubbed_as_well_as_values():
    """A slice value becomes a key here, and a slice value can be a person."""
    result = export_card(_card(fairness={"carol@example.com": {"disparity_exceeded": False}}))
    assert "carol@example.com" not in str(result.content)


def test_non_string_values_survive_unchanged():
    result = export_card(_card(metrics={"rmse": 12.5, "n": 100}))
    assert result.content["metrics"] == {"rmse": 12.5, "n": 100}


def test_a_clean_card_reports_no_redactions_and_is_safe():
    result = export_card(_card())
    assert result.redactions == []
    assert result.safe is True


# ── a secret blocks, and is never echoed ──────────────────────────────────────


def test_a_secret_makes_the_export_unsafe():
    result = export_card(_card(limitations="key AKIAIOSFODNN7EXAMPLE"))
    assert result.safe is False
    assert result.secret_findings[0]["rule"] == "aws-access-key"


def test_a_secret_is_reported_by_rule_and_never_echoed_in_full():
    result = export_card(_card(limitations="key AKIAIOSFODNN7EXAMPLE"))
    assert "AKIAIOSFODNN7EXAMPLE" not in str(result.secret_findings)


def test_export_card_never_raises_it_reports():
    """The function scrubs and reports; refusing is the caller's decision."""
    assert export_card(_card(limitations="AKIAIOSFODNN7EXAMPLE")).content["model"] == "JPCP"


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_the_cli_refuses_an_export_containing_a_secret(monkeypatch):
    monkeypatch.setattr(
        "examlops.cards.build_model_card",
        lambda m, **kw: type(
            "C", (), {"as_dict": lambda self: _card(limitations="AKIAIOSFODNN7EXAMPLE")}
        )(),
    )
    result = runner.invoke(cards_a6_cmd.app, ["export", "JPCP"])
    assert result.exit_code == 1
    assert "Refusing to export" in result.output
    assert "aws-access-key" in result.output


def test_the_refusal_is_audited(monkeypatch):
    from examlops.data.audit import export_audit_events

    monkeypatch.setattr(
        "examlops.cards.build_model_card",
        lambda m, **kw: type(
            "C", (), {"as_dict": lambda self: _card(limitations="AKIAIOSFODNN7EXAMPLE")}
        )(),
    )
    runner.invoke(cards_a6_cmd.app, ["export", "JPCP"])
    assert [e for e in export_audit_events() if e["action"] == "card_export_blocked"]


def test_force_publishes_and_records_that_it_was_forced(monkeypatch, tmp_path):
    from examlops.data.audit import export_audit_events

    monkeypatch.setattr(
        "examlops.cards.build_model_card",
        lambda m, **kw: type(
            "C", (), {"as_dict": lambda self: _card(limitations="AKIAIOSFODNN7EXAMPLE")}
        )(),
    )
    out = tmp_path / "card.json"
    result = runner.invoke(cards_a6_cmd.app, ["export", "JPCP", "--force", "--out", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    forced = [e for e in export_audit_events() if e["action"] == "card_exported"]
    assert forced and "true" in str(forced[-1]["details"]).lower()


def test_a_clean_export_writes_the_file_and_names_what_it_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "examlops.cards.build_model_card",
        lambda m, **kw: type("C", (), {"as_dict": lambda self: _card()})(),
    )
    out = tmp_path / "card.json"
    result = runner.invoke(cards_a6_cmd.app, ["export", "JPCP", "--out", str(out)])
    assert result.exit_code == 0
    assert "acme" not in out.read_text()
    assert "removed internal field: tenant" in result.output


def test_a_dataset_card_can_be_exported_too(monkeypatch, tmp_path):
    out = tmp_path / "ds.json"
    result = runner.invoke(cards_a6_cmd.app, ["export", "PM100", "--dataset", "--out", str(out)])
    assert result.exit_code == 0
    assert '"@type": "sc:Dataset"' in out.read_text()


# ── the bug this iteration tripped over ───────────────────────────────────────


def test_an_audit_event_can_be_the_first_touch_of_a_fresh_database(tmp_path, monkeypatch):
    """`write_audit_event` was the one path in `data.audit` that skipped `init_db()`.

    Every read path in that module bootstraps the schema; this write path did not, so a command
    whose first database touch was its own audit event died on `no such table: audit_events`.
    `exa cards export --dataset` is exactly such a command, which is how it surfaced.
    """
    import importlib

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "fresh.db"))
    import examlops.platform_db as pdb

    importlib.reload(pdb)
    pdb._INITIALIZED_PATHS.clear()
    from examlops.data.audit import export_audit_events, write_audit_event

    write_audit_event("test", "actor", "first_touch", "target", {"k": "v"})

    assert [e for e in export_audit_events() if e["action"] == "first_touch"]
