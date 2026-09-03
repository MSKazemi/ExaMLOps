"""What a suite run cost — and, more importantly, when it must refuse to say.

The scores here are read by `exa eval gate` as *ceilings* (`--metric tokens_per_answer:max=6000`),
which inverts the usual risk: a metric that silently reports too little passes the gate. Most of
these tests are therefore about the cases where no number may be recorded at all.
"""

from __future__ import annotations

import pytest

from examlops.evaluation.usage import (
    Usage,
    parse_usage,
    price,
    pricing_names,
    usage_scores,
)

# ── parse_usage ───────────────────────────────────────────────────────────────


def test_reads_the_openai_usage_block():
    u = parse_usage({"usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}})
    assert u == Usage(prompt_tokens=120, completion_tokens=30, total_tokens=150)


def test_a_response_without_usage_is_none_not_zero():
    """The bridge omits `usage` when the model reported no `usage_metadata`. That is 'unknown',
    and a Usage(0,0,0) would be read downstream as 'this answer was free'."""
    assert parse_usage({"choices": [{"message": {"content": "hi"}}]}) is None


def test_a_zero_filled_usage_block_is_also_none():
    """A backend that sends zeros instead of omitting the block is making the same statement."""
    assert (
        parse_usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
        is None
    )


def test_total_is_derived_when_the_backend_reports_only_the_parts():
    u = parse_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    assert u is not None and u.total_tokens == 15


def test_a_total_only_backend_still_counts():
    u = parse_usage({"usage": {"total_tokens": 42}})
    assert u is not None and u.total_tokens == 42


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"usage": None}, {"usage": "120"}, {"usage": []}],
)
def test_malformed_bodies_do_not_raise(payload):
    """A suite that already spent real tokens getting the answer must not die parsing it."""
    assert parse_usage(payload) is None


def test_nonsense_counts_are_dropped_rather_than_trusted():
    assert parse_usage({"usage": {"prompt_tokens": "abc", "completion_tokens": -5}}) is None


# ── usage_scores: the refusal rules ───────────────────────────────────────────


def test_no_usage_reported_records_nothing():
    """The rule that matters: summing to zero would make a `max=` budget gate pass at exactly
    the moment token reporting broke."""
    assert usage_scores([None, None, None], model="qwen3:8b") == {}


def test_an_empty_run_records_nothing():
    assert usage_scores([], model="gpt-4o") == {}


def test_totals_cover_only_the_answers_that_reported():
    scores = usage_scores(
        [Usage(100, 20, 120), None, Usage(200, 40, 240)],
        model="qwen3:8b",
    )
    assert scores["tokens_total"] == 360.0
    assert scores["prompt_tokens_total"] == 300.0
    assert scores["completion_tokens_total"] == 60.0


def test_per_answer_divides_by_what_reported_not_by_what_was_asked():
    """Dividing 360 tokens by three answers when only two carried usage would report a
    per-question spend a third below the truth — and pass a ceiling set from the real one."""
    scores = usage_scores([Usage(100, 20, 120), None, Usage(200, 40, 240)], model="qwen3:8b")
    assert scores["tokens_per_answer"] == 180.0


def test_partial_coverage_is_recorded_so_a_total_is_never_misread():
    scores = usage_scores([Usage(100, 20, 120), None, None, None], model="qwen3:8b")
    assert scores["usage_reported_rate"] == 0.25


def test_full_coverage_reports_one():
    scores = usage_scores([Usage(10, 5, 15), Usage(10, 5, 15)], model="qwen3:8b")
    assert scores["usage_reported_rate"] == 1.0


def test_answered_overrides_the_coverage_denominator():
    scores = usage_scores([Usage(10, 5, 15)], model="qwen3:8b", answered=4)
    assert scores["usage_reported_rate"] == 0.25


# ── pricing ───────────────────────────────────────────────────────────────────


def test_an_unpriced_local_model_records_tokens_but_no_dollars():
    """Skipper's normal backend is a local Ollama model. Charging it the rate table's
    unknown-model default would invent a dollar cost for self-hosted inference."""
    scores = usage_scores([Usage(1000, 500, 1500)], model="ollama:qwen3:8b")
    assert scores["tokens_total"] == 1500.0
    assert "cost_usd" not in scores
    assert "cost_per_answer" not in scores


def test_a_priced_model_records_cost():
    scores = usage_scores([Usage(1000, 1000, 2000)], model="gpt-4o")
    # 1000/1000 * 0.0025 + 1000/1000 * 0.01
    assert scores["cost_usd"] == pytest.approx(0.0125)
    assert scores["cost_per_answer"] == pytest.approx(0.0125)


def test_a_self_hosted_model_the_table_names_is_priced_at_zero():
    """Zero here is a measurement, not a missing value — the table says llama3.1:8b costs
    nothing per token because its cost is GPU-seconds."""
    scores = usage_scores([Usage(1000, 1000, 2000)], model="llama3.1:8b")
    assert scores["cost_usd"] == 0.0


def test_cost_is_split_across_the_answers_that_reported():
    scores = usage_scores([Usage(1000, 1000, 2000), Usage(1000, 1000, 2000)], model="gpt-4o")
    assert scores["cost_usd"] == pytest.approx(0.025)
    assert scores["cost_per_answer"] == pytest.approx(0.0125)


def test_price_returns_none_for_an_unknown_model():
    assert price("some-model-nobody-prices", 1000, 1000) is None


# ── pricing_names ─────────────────────────────────────────────────────────────


def test_a_backend_prefixed_name_still_finds_its_rate():
    """`model_version` is stored as `<backend>:<model>`, which is not how a rate table is keyed."""
    assert price("openai:gpt-4o", 1000, 1000) == pytest.approx(0.0125)


def test_a_model_name_containing_a_colon_is_matched_whole_first():
    """`llama3.1:8b` must not be read as backend `llama3.1` + model `8b`."""
    assert pricing_names("llama3.1:8b")[0] == "llama3.1:8b"
    assert price("llama3.1:8b", 1000, 1000) == 0.0


def test_a_bare_name_has_one_candidate():
    assert pricing_names("gpt-4o") == ["gpt-4o"]
