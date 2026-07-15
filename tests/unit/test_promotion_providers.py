"""Unit tests for pluggable promotion providers (INC-5 / ADR 0077).

Verifies:
* the default ``threshold`` provider is byte-identical to the previous inline logic;
* all four comparison operators work correctly;
* a declarative expression formula overrides evaluation with zero core edits;
* graceful degradation when the provider fails;
* ``exa providers list`` sees the promotion domain.
"""

from __future__ import annotations

import pytest

from examlops.promotion_providers import (
    ThresholdPromotionProvider,
    register_builtins,
    resolve_promotion_eval_fn,
)
from examlops.providers import default_provider_name, list_providers


# ── ThresholdPromotionProvider.compute ────────────────────────────────────────

@pytest.mark.parametrize(
    "metric_val,threshold,operator,expected_passes",
    [
        (3.0, 5.0, "lt", True),    # 3 < 5
        (5.0, 5.0, "lt", False),   # 5 is not < 5
        (5.0, 5.0, "lte", True),   # 5 <= 5
        (6.0, 5.0, "gt", True),    # 6 > 5
        (5.0, 5.0, "gt", False),   # 5 is not > 5
        (5.0, 5.0, "gte", True),   # 5 >= 5
        (4.9, 5.0, "gte", False),  # 4.9 is not >= 5
    ],
)
def test_threshold_provider_operators(metric_val, threshold, operator, expected_passes):
    p = ThresholdPromotionProvider()
    out = p.compute({"metric_val": metric_val, "threshold": threshold, "operator": operator})
    assert out["passes"] == expected_passes


def test_threshold_provider_unknown_operator():
    p = ThresholdPromotionProvider()
    out = p.compute({"metric_val": 1.0, "threshold": 2.0, "operator": "neq"})
    assert out["passes"] is False
    assert "unknown operator" in out["reason"]


def test_threshold_provider_reason_format():
    p = ThresholdPromotionProvider()
    out = p.compute({"metric_val": 3.0, "threshold": 5.0, "operator": "lt"})
    assert "<" in out["reason"]
    assert "3.0000" in out["reason"]


# ── resolve_promotion_eval_fn ─────────────────────────────────────────────────

def test_default_eval_matches_inline_logic():
    """resolve_promotion_eval_fn() must be byte-identical to the pre-provider inline ops."""
    _OPS = {
        "lt": lambda v, t: v < t,
        "gt": lambda v, t: v > t,
        "lte": lambda v, t: v <= t,
        "gte": lambda v, t: v >= t,
    }
    eval_fn = resolve_promotion_eval_fn()
    cases = [
        (3.0, 5.0, "lt"),
        (5.0, 5.0, "lt"),
        (5.0, 5.0, "lte"),
        (6.0, 5.0, "gt"),
        (5.0, 5.0, "gte"),
    ]
    for metric_val, threshold, operator in cases:
        passes_got, _ = eval_fn(metric_val, threshold, operator)
        passes_exp = _OPS[operator](metric_val, threshold)
        assert passes_got == passes_exp, f"mismatch: {metric_val} {operator} {threshold}"


def test_eval_returns_bool():
    eval_fn = resolve_promotion_eval_fn()
    passes, reason = eval_fn(3.0, 5.0, "lt")
    assert isinstance(passes, bool)
    assert isinstance(reason, str)


# ── expression formula overrides evaluation (zero core edits) ─────────────────

def test_expression_formula_always_passes():
    """A declarative formula returning passes=1 passes through the substrate."""
    from examlops.providers import get_provider

    provider = get_provider(
        "promotion",
        "expression",
        config={"formulas": {"passes": "1", "reason": "0"}},
    )
    out = provider.compute({"metric_val": 99.0, "threshold": 1.0, "operator": "lt"})
    assert float(out["passes"]) == pytest.approx(1.0)


# ── graceful degradation ──────────────────────────────────────────────────────

def test_broken_provider_degrades_to_inline(monkeypatch):
    import examlops.promotion_providers as mod

    monkeypatch.setattr(mod, "resolve_provider", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    eval_fn = mod.resolve_promotion_eval_fn()
    passes, _ = eval_fn(3.0, 5.0, "lt")
    assert passes is True


def test_provider_compute_error_degrades(monkeypatch):
    import examlops.promotion_providers as mod

    class Exploding:
        def compute(self, inputs):
            raise ValueError("bad formula")

    monkeypatch.setattr(mod, "resolve_provider", lambda *a, **k: Exploding())
    eval_fn = mod.resolve_promotion_eval_fn()
    passes, _ = eval_fn(3.0, 5.0, "lt")
    assert passes is True  # 3 < 5 in the degraded inline path


# ── registry ──────────────────────────────────────────────────────────────────

def test_registration_is_idempotent_and_default():
    register_builtins()
    register_builtins()  # idempotent
    names = {i.name for i in list_providers("promotion")}
    assert "threshold" in names
    assert default_provider_name("promotion") == "threshold"
