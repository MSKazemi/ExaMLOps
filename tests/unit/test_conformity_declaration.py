# tests/unit/test_conformity_declaration.py
"""ADR 0012 clause 4 — the Annex-V EU Declaration of Conformity.

The clause names "a per-system state machine … **with a Declaration-of-Conformity template**".
The state machine shipped; nothing generated the document, so the artifact the whole conformity
workflow exists to produce did not exist.

Annex V requires eight items. Five of them are statements only the provider can make, and the
tests below are mostly about the platform *not* making them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli.commands import compliance_cmd  # noqa: E402
from examlops.compliance import (  # noqa: E402
    PROVIDER_FIELDS,
    generate_declaration,
    set_conformity_state,
)
from examlops.data.governance import (  # noqa: E402
    ANNEX_IV,
    DECLARATION,
    list_technical_files,
    save_technical_file,
    set_compliance_system,
)

runner = CliRunner()

_PROVIDER = {
    "provider": "Forschungszentrum Example GmbH",
    "provider_address": "Example Str. 1, 52425 Example, DE",
    "signatory": "A. Person",
    "signatory_function": "Head of AI Governance",
}
_ISSUED = "Example, 2026-09-02"


def _ready(model: str = "JPCP") -> str:
    """A system that has cleared every blocker: declared, documented, no gaps."""
    set_compliance_system(model, risk_tier="high")
    save_technical_file(model, "# technical file", gaps=0)
    for state in ("documented", "assessed", "declared"):
        set_conformity_state(model, state, "tester")
    return model


def _final(model: str):
    return generate_declaration(model, issued_at=_ISSUED, **_PROVIDER)


# ── the platform never states what only the provider can ──────────────────────


def test_a_bare_system_produces_a_draft_listing_every_reason():
    doc = generate_declaration("Fresh", issued_at=_ISSUED)
    assert doc.draft is True
    assert len(doc.blockers) == 3  # state, provider fields, no technical file
    assert "not 'declared'" in doc.blockers[0]


def test_missing_provider_fields_are_placeholders_never_invented():
    doc = generate_declaration("Fresh", issued_at=_ISSUED)
    md = doc.to_markdown()
    assert md.count("TO BE COMPLETED BY THE PROVIDER") >= len(PROVIDER_FIELDS)
    for field_name in PROVIDER_FIELDS:
        assert getattr(doc, field_name) is None


def test_a_draft_says_so_on_its_face():
    """A document that reads as final while resting on nothing is the whole risk here."""
    md = generate_declaration("Fresh", issued_at=_ISSUED).to_markdown()
    assert "DRAFT — NOT A DECLARATION" in md
    assert "## Why this is a draft" in md


def test_the_non_legal_advice_disclaimer_is_on_every_declaration():
    for doc in (generate_declaration("Fresh", issued_at=_ISSUED), _final(_ready("D1"))):
        assert "NOT legal advice" in doc.to_markdown()


def test_the_issue_date_is_required_and_not_defaulted_to_now():
    """The issue date is a fact about when a person signed, not when a generator ran."""
    with pytest.raises(TypeError):
        generate_declaration("Fresh")  # type: ignore[call-arg]


# ── what makes a declaration final ────────────────────────────────────────────


def test_a_fully_prepared_system_yields_a_final_declaration():
    doc = _final(_ready("D2"))
    assert doc.blockers == []
    assert doc.draft is False
    assert "**Status: FINAL**" in doc.to_markdown()


def test_a_state_short_of_declared_keeps_it_a_draft():
    set_compliance_system("D3", risk_tier="high")
    save_technical_file("D3", "# tf", gaps=0)
    set_conformity_state("D3", "documented", "tester")
    doc = generate_declaration("D3", issued_at=_ISSUED, **_PROVIDER)
    assert doc.draft is True
    assert any("not 'declared'" in b for b in doc.blockers)


def test_a_technical_file_with_gaps_keeps_it_a_draft():
    """A declaration resting on an incomplete technical file must not look final."""
    set_compliance_system("D4", risk_tier="high")
    save_technical_file("D4", "# tf", gaps=3)
    for state in ("documented", "assessed", "declared"):
        set_conformity_state("D4", state, "tester")
    doc = generate_declaration("D4", issued_at=_ISSUED, **_PROVIDER)
    assert doc.draft is True
    assert any("3 evidence gap" in b for b in doc.blockers)


def test_one_missing_provider_field_is_enough_to_keep_it_a_draft():
    model = _ready("D5")
    partial = {**_PROVIDER}
    partial.pop("signatory_function")
    doc = generate_declaration(model, issued_at=_ISSUED, **partial)
    assert doc.draft is True
    assert "signatory_function" in doc.blockers[0]


# ── Annex V content ───────────────────────────────────────────────────────────


def test_all_eight_annex_v_sections_are_present():
    md = _final(_ready("D6")).to_markdown()
    for heading in (
        "## 1. AI system identification",
        "## 2. Provider",
        "## 3. Responsibility",
        "## 4. Conformity statement",
        "## 5. Personal data",
        "## 6. Standards and common specifications",
        "## 7. Notified body",
        "## 8. Signature",
    ):
        assert heading in md, heading


def test_the_conformity_statement_is_made_by_the_provider_not_the_platform():
    md = _final(_ready("D7")).to_markdown()
    assert "The provider declares that the AI system" in md
    assert "issued under the sole responsibility of the provider" in md


def test_no_personal_data_statement_is_made_unless_declared():
    """Annex V(5) applies only where personal data is processed; asserting GDPR compliance
    for a system that does not process any would be a claim nobody made."""
    without = _final(_ready("D8")).to_markdown()
    assert "does not process personal data" in without
    assert "2016/679" not in without

    with_pd = generate_declaration(
        _ready("D9"), issued_at=_ISSUED, processes_personal_data=True, **_PROVIDER
    ).to_markdown()
    assert "2016/679" in with_pd


def test_an_absent_notified_body_is_not_applicable_not_a_placeholder():
    """Annex V(7) is 'where applicable'; most systems have none, which is an answer."""
    assert "Not applicable" in _final(_ready("D10")).to_markdown()


def test_the_declaration_is_traceable_to_the_technical_file_it_rests_on():
    model = _ready("D11")
    md = _final(model).to_markdown()
    assert "Annex-IV technical file v1" in md
    assert "risk tier" in md.lower()


# ── storage ───────────────────────────────────────────────────────────────────


def test_declarations_and_technical_files_keep_separate_version_sequences():
    """One shared sequence would number two documents about the same system misleadingly."""
    save_technical_file("D12", "# tf a", gaps=0)
    save_technical_file("D12", "# tf b", gaps=0)
    assert save_technical_file("D12", "# doc", kind=DECLARATION) == 1
    assert [r["version"] for r in list_technical_files("D12")] == [2, 1]
    assert [r["version"] for r in list_technical_files("D12", kind=DECLARATION)] == [1]


def test_a_row_written_before_the_kind_column_reads_as_a_technical_file():
    save_technical_file("D13", "# tf", gaps=0)
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute("UPDATE technical_files SET kind=NULL WHERE model='D13'")
    assert len(list_technical_files("D13", kind=ANNEX_IV)) == 1
    assert list_technical_files("D13", kind=DECLARATION) == []


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_the_cli_persists_the_declaration_and_audits_it():
    model = _ready("D14")
    result = runner.invoke(
        compliance_cmd.app,
        [
            "declaration",
            model,
            "--issued-at",
            _ISSUED,
            "--provider",
            _PROVIDER["provider"],
            "--provider-address",
            _PROVIDER["provider_address"],
            "--signatory",
            _PROVIDER["signatory"],
            "--signatory-function",
            _PROVIDER["signatory_function"],
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(list_technical_files(model, kind=DECLARATION)) == 1

    from examlops.data.audit import export_audit_events

    events = [e for e in export_audit_events() if e["action"] == "conformity_declaration_generated"]
    assert events and "false" in str(events[-1]["details"]).lower()  # draft: False


def test_the_cli_warns_for_every_blocker_on_a_draft(tmp_path):
    out = tmp_path / "doc.md"
    result = runner.invoke(
        compliance_cmd.app,
        ["declaration", "Fresh2", "--issued-at", _ISSUED, "--out", str(out)],
    )
    assert result.exit_code == 0
    assert "DRAFT" in result.output
    assert "not 'declared'" in result.output
    assert "TO BE COMPLETED BY THE PROVIDER" in out.read_text()
