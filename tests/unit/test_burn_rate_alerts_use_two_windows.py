"""A burn-rate alert must actually consult both of its windows (C6 / ADR 0023).

Multi-window burn-rate alerting exists for one reason: a short window alone is too jumpy to
page on. The pattern requires the error rate to be high over a *short* window **and** still
high over a *long* one, so a momentary spike is not a page and a slow leak is still caught.
Take the long window away and what is left is a single-window alert wearing a two-window name.

That is what these rules were: ``generate_rules`` built ``err_short`` and ``err_long`` from the
same string, so every alert's expression was ``(X > t) and (X > t)``, which is just ``X > t``.
The window durations survived only in the alert's *name* and its annotation prose — the alert
said "over 5m+1h" and consulted neither. ``promtool check rules`` accepts it, because it is
valid PromQL that means something; it just does not mean what the alert claims.

Nothing catches this by counting rules or by checking severities, so it is checked here: for
every generated alert, the two sides of the ``and`` must differ, and each side must name the
window the annotation promises.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

SPEC = {"model": "M", "name": "s", "target": 0.99, "window": "30d"}


def _alerts(spec=None):
    from examlops.slo import generate_rules

    rules = generate_rules(spec or SPEC)
    alert_groups = [g for g in rules.groups if any("alert" in r for r in g["rules"])]
    assert alert_groups, "no alert group was generated at all"
    return [r for g in alert_groups for r in g["rules"]]


def _operands(expr: str) -> list[str]:
    """Split a two-window burn-rate expression on its top-level ``and``."""
    parts = re.split(r"\s+and\s+", expr)
    return [p.strip() for p in parts]


def test_the_two_sides_of_the_and_are_not_the_same_condition():
    """``(X > t) and (X > t)`` cannot fire on anything ``X > t`` alone would not."""
    for rule in _alerts():
        sides = _operands(rule["expr"])
        assert len(sides) == 2, f"{rule['alert']}: expected a two-window expression, got {sides}"
        assert sides[0] != sides[1], (
            f"{rule['alert']}: both sides of the `and` are identical, so the long window "
            f"is not consulted and the alert is single-window:\n  {rule['expr']}"
        )


def test_each_alert_consults_the_two_windows_its_annotation_promises():
    """The durations in the prose must be the durations in the expression."""
    for rule in _alerts():
        # The summary ends with "(<short>/<long>)" — that is the claim being audited.
        m = re.search(r"\(([0-9]+[smhd])/([0-9]+[smhd])\)", rule["annotations"]["summary"])
        assert m, f"{rule['alert']}: summary does not name its two windows"
        short_w, long_w = m.group(1), m.group(2)
        assert short_w != long_w, f"{rule['alert']}: the two windows are the same duration"
        for w in (short_w, long_w):
            assert f"[{w}]" in rule["expr"] or f"[{w}:" in rule["expr"], (
                f"{rule['alert']}: annotation promises a {w} window, but the expression "
                f"never ranges over it:\n  {rule['expr']}"
            )


def test_the_slow_burn_alert_ranges_over_days_not_an_instant():
    """The ticket-grade alert is the one a single bad scrape must never trigger."""
    slow = [r for r in _alerts() if r["labels"]["severity"] == "warning"]
    assert slow, "no warning-severity burn-rate alert was generated"
    assert any("[3d]" in r["expr"] or "[3d:" in r["expr"] for r in slow), (
        "the slowest burn-rate alert never ranges over its 3d window; "
        "it would fire on instantaneous error ratio like the fast-burn one"
    )


def test_every_window_pair_produces_a_distinct_expression():
    """Four alerts that evaluate the same series differ only in threshold and `for`."""
    exprs = [r["expr"] for r in _alerts()]
    assert len(set(exprs)) == len(exprs), (
        "two burn-rate alerts generated the identical expression — the window pair "
        "is not reaching the query:\n  " + "\n  ".join(sorted(exprs))
    )


def test_a_custom_sli_query_still_yields_two_distinct_windows():
    """A user-supplied ratio must be windowed too, not just the default recorded one."""
    spec = dict(SPEC, sli_query="sum(rate(good[5m])) / sum(rate(total[5m]))")
    for rule in _alerts(spec):
        sides = _operands(rule["expr"])
        assert len(sides) == 2 and sides[0] != sides[1], (
            f"{rule['alert']}: custom sli_query collapsed back to one window:\n  {rule['expr']}"
        )


@pytest.mark.parametrize("field", ["for", "labels", "annotations"])
def test_the_alert_keeps_the_shape_prometheus_requires(field):
    for rule in _alerts():
        assert field in rule, f"{rule['alert']}: missing `{field}`"
