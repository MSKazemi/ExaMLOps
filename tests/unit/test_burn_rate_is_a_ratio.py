"""A burn-rate alert that is not dimensionless fires on everything and means nothing.

Burn rate is defined as a ratio over a ratio: the observed error ratio divided by the error
budget (1 - SLO). `SLOErrorBudgetFastBurn` and `SLOErrorBudgetSlowBurn` instead divided an error
*rate* by "budget per second" — errors/second over fraction/second — and compared the result
against 14.4 and 3. The threshold worked out at one error every 417 days, so **a single error
anywhere in the window scored 144000x and paged `critical`**, and a service comfortably inside
its SLO (0.1% errors against a 0.5% budget, a true burn rate of 0.2x) reported 5184000x. The
description printed that number to the operator as the burn rate.

promtool cannot see this: the expression is valid PromQL, the metric exists, the rule parses.
Nor can a test that greps the expression, because the broken form contains every token the fixed
one does. The only check that distinguishes them is to **evaluate** the expression — so this
guard substitutes traffic numbers into it and asserts which scenarios fire.

The repo already had the right form in two places: `examlops.slo` generates
`avg_over_time(<ratio>[w]) > factor * budget`, and `slo_status` computes
`observed_error / budget_total`. Only the hand-written static rules disagreed, which is the
usual way this arrives — a second copy that no test ever compared against the first.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_RULES = (
    Path(__file__).resolve().parents[2]
    / "platform"
    / "infra"
    / "docker-compose"
    / "alert_rules.yml"
)

_METRIC = "ray_examlops_predict_requests_total"
_ERROR_BUDGET = 0.005  # 1 - 0.995, the 30-day SLO these rules are written against

# alert -> the burn-rate multiplier its own summary advertises
_BURN_ALERTS = {"SLOErrorBudgetFastBurn": 14.4, "SLOErrorBudgetSlowBurn": 3.0}


def _exprs() -> dict[str, str]:
    rules = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    return {
        r["alert"]: " ".join(r["expr"].split())
        for g in rules.get("groups", [])
        for r in g.get("rules", [])
        if r.get("alert") in _BURN_ALERTS
    }


def _evaluate(expr: str, errors_per_sec: float, total_per_sec: float) -> bool:
    """Evaluate a burn-rate expression for a given traffic mix. Returns whether it fires.

    Only the two `sum(rate(...))` terms and `clamp_min` carry meaning here; everything else in
    these expressions is plain arithmetic, so Python can finish the job.
    """
    # The selector with a `status=~...` filter is the error stream; the bare one is all traffic.
    expr = re.sub(
        re.escape(_METRIC) + r"\{[^}]*\}\[[0-9a-z]+\]", f"__ERR__[{errors_per_sec}]", expr
    )
    expr = re.sub(re.escape(_METRIC) + r"\[[0-9a-z]+\]", f"__TOT__[{total_per_sec}]", expr)
    expr = re.sub(r"sum\(rate\(__ERR__\[([0-9.e+-]+)\]\)\)", r"\1", expr)
    expr = re.sub(r"sum\(rate\(__TOT__\[([0-9.e+-]+)\]\)\)", r"\1", expr)
    assert _METRIC not in expr and "rate(" not in expr, f"unsubstituted terms left in: {expr}"
    return bool(eval(expr, {"__builtins__": {}}, {"clamp_min": max}))  # noqa: S307


# (label, errors/sec, total/sec, should_fire_fast, should_fire_slow)
_SCENARIOS = [
    ("a perfectly healthy service", 0.0, 10.0, False, False),
    ("no traffic at all", 0.0, 0.0, False, False),
    # The regression. One error in the whole window, against real traffic.
    ("one stray error in an hour", 1 / 3600, 10.0, False, False),
    # Inside the SLO: 0.1% errors against a 0.5% budget is a true burn rate of 0.2x.
    ("0.1% errors — inside the SLO", 0.01, 10.0, False, False),
    # 2% errors = 4x budget: over the slow threshold (1.5%), under the fast one (7.2%).
    # Deliberately not 1.5% exactly — the comparison is strict, and a scenario sitting on the
    # boundary tests the boundary rather than the behaviour.
    ("2% errors — 4x budget", 0.2, 10.0, False, True),
    # 10% errors = 20x budget: both.
    ("10% errors — a real outage", 1.0, 10.0, True, True),
]


@pytest.mark.parametrize("name,err,tot,fast,slow", _SCENARIOS)
def test_burn_rate_alerts_fire_only_when_the_budget_is_actually_burning(name, err, tot, fast, slow):
    exprs = _exprs()
    assert set(exprs) == set(_BURN_ALERTS), f"expected both burn alerts, found {sorted(exprs)}"
    for alert, expected in (("SLOErrorBudgetFastBurn", fast), ("SLOErrorBudgetSlowBurn", slow)):
        got = _evaluate(exprs[alert], err, tot)
        assert got is expected, (
            f"{alert} {'fired' if got else 'stayed quiet'} for {name} "
            f"({err} errors/s of {tot} req/s); expected it to "
            f"{'fire' if expected else 'stay quiet'}."
        )


def test_the_threshold_matches_the_multiplier_the_alert_advertises():
    """A burn-rate threshold is `multiplier x error budget` — and the summary names the multiplier.

    This is what keeps the number in the operator-facing text and the number in the expression
    from drifting apart, which is the other half of how the broken rule stayed plausible.
    """
    rules = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    checked = 0
    for g in rules.get("groups", []):
        for r in g.get("rules", []):
            if r.get("alert") not in _BURN_ALERTS:
                continue
            multiplier = _BURN_ALERTS[r["alert"]]
            threshold = float(re.search(r">\s*([0-9.]+)\s*$", " ".join(r["expr"].split()))[1])
            assert threshold == pytest.approx(multiplier * _ERROR_BUDGET), (
                f"{r['alert']} compares against {threshold}, but a {multiplier}x burn of a "
                f"{_ERROR_BUDGET} budget is {multiplier * _ERROR_BUDGET}"
            )
            assert f"{multiplier:g}" in r["annotations"]["summary"]
            checked += 1
    assert checked == len(_BURN_ALERTS), f"only checked {checked} burn alerts"
