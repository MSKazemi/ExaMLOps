"""ADR 0035 clause 1 — a response asked for with a schema is a validated object.

The recorded gap: "the gateway's own request path never validates a response, so tool-call
arguments and RAG citations do not use it", and `generate_structured` — written for exactly this
path — "has no caller outside its own tests". The constrained-decoding half of the clause is still
absent, so the guarantee is reached the other way: parse, validate, repair, or raise. A caller gets
a valid object or a typed error, never unchecked text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.structured import StructuredOutputError  # noqa: E402

SCHEMA = {
    "type": "object",
    "required": ["name", "score"],
    "properties": {"name": {"type": "string"}, "score": {"type": "number"}},
}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "off")
    init_db()


def _client(text, cache=None):
    router = gw.Router()
    router.add_route("m", [("p", lambda model, messages, **kw: gw.Completion(text, model, ""))])
    return gw.GatewayClient(router=router, cache_store=cache)


def _chat(text, **kw):
    return _client(text).chat("m", [{"role": "user", "content": "hi"}], **kw)


# ── the happy path, and the shapes models actually emit ───────────────────────


def test_a_valid_object_is_parsed_and_attached():
    comp = _chat('{"name": "JPCP", "score": 0.9}', response_schema=SCHEMA)
    assert comp.parsed == {"name": "JPCP", "score": 0.9}


def test_a_fenced_object_is_still_parsed():
    """Every instruction-tuned model wraps JSON in a fence regardless of the prompt. Reporting a
    schema failure for a response that contains a perfectly good object would spend the repair
    budget on a formatting habit."""
    comp = _chat('```json\n{"name": "JPCP", "score": 0.9}\n```', response_schema=SCHEMA)
    assert comp.parsed["name"] == "JPCP"


def test_prose_around_the_object_is_tolerated():
    comp = _chat(
        'Sure! Here you go:\n{"name": "JPCP", "score": 0.9}\nHope that helps.',
        response_schema=SCHEMA,
    )
    assert comp.parsed["score"] == 0.9


# ── the guarantee ─────────────────────────────────────────────────────────────


def test_text_that_is_not_json_raises_rather_than_returning():
    with pytest.raises(StructuredOutputError, match="not JSON"):
        _chat("I'm afraid I can't do that.", response_schema=SCHEMA)


def test_an_object_that_cannot_be_repaired_raises():
    with pytest.raises(StructuredOutputError):
        _chat('{"name": "JPCP", "score": "not-a-number"}', response_schema=SCHEMA, max_repairs=0)


def test_no_schema_means_no_parsing_and_no_failure():
    """`parsed` is None because none was asked for — never because validation failed, which
    raises."""
    comp = _chat("free text, not JSON at all")
    assert comp.parsed is None and comp.text.startswith("free text")


# ── ordering, which is silent when wrong ──────────────────────────────────────


def test_the_validated_object_is_the_redacted_one(monkeypatch):
    """Enforcement runs after the guardrail. If it ran before, the object handed back could
    contain the very text the guardrail then removed from `text` — two views of one response
    disagreeing about what it said."""
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "enforce")
    comp = _chat('{"name": "mail bob@example.com", "score": 1}', response_schema=SCHEMA)
    assert "bob@example.com" not in comp.text
    assert "bob@example.com" not in comp.parsed["name"]


def test_an_invalid_response_is_never_cached():
    stored: list = []
    with pytest.raises(StructuredOutputError):
        _client("not json", cache=lambda m, msgs, c: stored.append(c)).chat(
            "m", [{"role": "user", "content": "hi"}], response_schema=SCHEMA
        )
    assert stored == []


def test_a_valid_response_is_cached_with_its_object():
    stored: list = []
    _client('{"name": "x", "score": 1}', cache=lambda m, msgs, c: stored.append(c)).chat(
        "m", [{"role": "user", "content": "hi"}], response_schema=SCHEMA
    )
    assert stored and stored[0].parsed == {"name": "x", "score": 1}


# ── metering (clause 4) ───────────────────────────────────────────────────────


def test_outcomes_are_metered_so_a_failure_rate_exists():
    from examlops.data.events import structured_output_stats

    _chat('{"name": "x", "score": 1}', response_schema=SCHEMA)
    with pytest.raises(StructuredOutputError):
        _chat("not json", response_schema=SCHEMA)
    stats = structured_output_stats()
    assert stats.get("valid") == 1, stats
    assert stats.get("failed") == 1, "a schema failure left no trace to build a rate from"


def test_generate_structured_now_has_a_production_caller():
    """It was written for this path and wired to nothing — the ADR's own finding. This asserts
    the gateway routes through it rather than re-implementing validate-then-repair, so the
    failure rate is metered from one place."""
    import inspect

    src = inspect.getsource(gw._enforce_schema)
    assert "generate_structured(" in src
