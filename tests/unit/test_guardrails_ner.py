# tests/unit/test_guardrails_ner.py
"""ADR 0026 clause 1 — Presidio NER supplement to the regex PII detectors.

`detect_pii`/`redact_pii` stay regex-only by default (see `test_guardrails.py`'s pinned "not
detected" list). This file covers the opt-in NER path: the wiring is tested with a stub engine
(no real Presidio install needed, so this runs in the fast suite); the bottom class re-verifies
with the *real* library and is skipped unless it is actually installed with a spaCy model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import guardrails as g  # noqa: E402


class _FakeResult:
    def __init__(self, entity_type: str, start: int, end: int) -> None:
        self.entity_type = entity_type
        self.start = start
        self.end = end


class _FakeEngine:
    """Duck-types Presidio's `AnalyzerEngine.analyze` for the wiring tests below."""

    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, list[str]]] = []

    def analyze(self, *, text: str, language: str, entities: list[str]) -> list[_FakeResult]:
        self.calls.append((text, entities))
        return self._results


@pytest.fixture(autouse=True)
def _clear_ner_cache(monkeypatch):
    # `_ner_engine` caches its result across calls; each test needs a clean slate.
    monkeypatch.setattr(g, "_ner_cache", {})
    yield
    monkeypatch.setattr(g, "_ner_cache", {})


def test_ner_is_off_by_default(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_GUARDRAIL_PII_NER", raising=False)
    assert g._ner_engine() is None
    assert g._ner_findings("Mario Rossi lives in Rome") == []


def test_an_unavailable_presidio_degrades_to_no_ner(monkeypatch):
    """The flag is on but the package/model is missing — never raise, just skip NER."""
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_PII_NER", "1")

    def _boom(*_a, **_k):
        raise ImportError("no module named presidio_analyzer")

    monkeypatch.setattr(g, "_ner_engine", lambda: None)
    assert g._ner_findings("Mario Rossi lives in Rome") == []
    # detect_pii/redact_pii must not raise either, and behave exactly as regex-only.
    assert g.detect_pii("contact alice@example.com") == {"email": ["alice@example.com"]}
    redacted, found = g.redact_pii("contact alice@example.com")
    assert found == ["email"]
    assert "alice@example.com" not in redacted


def test_ner_findings_are_merged_into_detect_pii(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_PII_NER", "1")
    text = "Mario Rossi emailed alice@example.com"
    start = text.index("Mario Rossi")
    end = start + len("Mario Rossi")
    fake = _FakeEngine([_FakeResult("PERSON", start, end)])
    monkeypatch.setattr(g, "_ner_engine", lambda: fake)

    found = g.detect_pii(text)
    assert found["person"] == ["Mario Rossi"]
    assert found["email"] == ["alice@example.com"]  # the regex detector is untouched
    # Only the NER-only entity types are requested — email/phone/etc. stay the regex detectors'.
    assert fake.calls[0][1] == list(g._NER_ENTITY_TYPES)


def test_ner_findings_are_redacted_without_disturbing_regex_offsets(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_PII_NER", "1")
    text = "Mario Rossi emailed alice@example.com about it"
    # redact_pii runs NER on the ALREADY-regex-redacted text, so the fake's offsets must be
    # computed against that (email already replaced), not the original string.
    after_regex = text.replace("alice@example.com", "[redacted-email]")
    start = after_regex.index("Mario Rossi")
    end = start + len("Mario Rossi")
    fake = _FakeEngine([_FakeResult("PERSON", start, end)])
    monkeypatch.setattr(g, "_ner_engine", lambda: fake)

    redacted, found = g.redact_pii(text)
    assert redacted == "[redacted-person] emailed [redacted-email] about it"
    assert set(found) == {"person", "email"}


def test_multiple_ner_findings_replace_back_to_front(monkeypatch):
    """Two findings must not shift each other's offsets — replace from the highest start first."""
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_PII_NER", "1")
    text = "Mario Rossi met Anna Bianchi in Rome"
    m_start, m_end = text.index("Mario Rossi"), text.index("Mario Rossi") + len("Mario Rossi")
    a_start, a_end = text.index("Anna Bianchi"), text.index("Anna Bianchi") + len("Anna Bianchi")
    r_start, r_end = text.index("Rome"), text.index("Rome") + len("Rome")
    fake = _FakeEngine(
        [
            _FakeResult("PERSON", m_start, m_end),
            _FakeResult("PERSON", a_start, a_end),
            _FakeResult("LOCATION", r_start, r_end),
        ]
    )
    monkeypatch.setattr(g, "_ner_engine", lambda: fake)

    redacted, found = g.redact_pii(text)
    assert redacted == "[redacted-person] met [redacted-person] in [redacted-location]"
    assert set(found) == {"person", "location"}


@pytest.mark.parametrize(
    ("value", "should_attempt"),
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False)],
)
def test_the_env_var_accepts_the_usual_truthy_spellings(monkeypatch, caplog, value, should_attempt):
    """Force the `presidio_analyzer` import to fail deterministically (`sys.modules` poisoning),
    independent of whether the real package happens to be installed in whatever environment runs
    this test. The observable difference between "off" and "on but unavailable" is then whether
    `_ner_engine` even *tries* the import at all — logged as the unavailable-package warning.
    """
    import sys

    monkeypatch.setitem(sys.modules, "presidio_analyzer", None)
    if value:
        monkeypatch.setenv("EXAMLOPS_GUARDRAIL_PII_NER", value)
    else:
        monkeypatch.delenv("EXAMLOPS_GUARDRAIL_PII_NER", raising=False)
    with caplog.at_level("WARNING", logger="examlops.guardrails"):
        assert g._ner_engine() is None
    attempted = any("Presidio is unavailable" in r.message for r in caplog.records)
    assert attempted is should_attempt


# ── Real Presidio, opt-in ──────────────────────────────────────────────────────────────────────

_MODEL = "en_core_web_sm"


def _presidio_ready() -> bool:
    try:
        import spacy  # noqa: PLC0415

        spacy.load(_MODEL)
    except Exception:
        return False
    return True


@pytest.mark.live
@pytest.mark.skipif(
    not _presidio_ready(), reason=f"presidio_analyzer + spaCy model {_MODEL!r} not installed"
)
def test_real_presidio_detects_a_name_regex_cannot(monkeypatch):
    monkeypatch.setattr(g, "_ner_cache", {})
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_PII_NER", "1")
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_PRESIDIO_MODEL", _MODEL)
    try:
        found = g.detect_pii("My name is John Smith and I live in Berlin.")
        assert "John Smith" in found.get("person", [])
    finally:
        monkeypatch.setattr(g, "_ner_cache", {})
