"""An error *rate* must divide by the events the service was actually asked to serve.

`ray_examlops_predict_requests_total` carries six statuses, and two of them — `invalid` (a malformed
request) and `not_found` (a model that does not exist) — are the service answering **correctly**
about a caller's mistake. They are not failures, and they are not valid events either.

Putting them in the denominator makes the error rate a function of how much junk the callers send:

| | requests | server errors | error rate |
|---|---|---|---|
| real traffic | 100 | 5 | **5.0%** |
| the same, plus 900 malformed requests | 1000 | 5 | **0.5%** |

So a burst of client garbage *improves* the SLO and *suppresses* the burn-rate alerts — the alerts
that exist to page during an outage can be silenced by unrelated traffic. That is the opposite of a
safe failure mode, and it applied to four alert rules and five Grafana panels.

The rule these hold: **wherever the bad-event numerator names statuses, the denominator must name
them too.** Dividing by the bare counter is the defect; the SLI's denominator is
`success|error|timeout|deadline_exceeded`, the events that represent a request the service accepted
and had to answer.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "platform/infra/docker-compose/alert_rules.yml"
DASHBOARDS = ROOT / "platform/infra/docker-compose/grafana/provisioning/dashboards"
SERVING_COUNTER = "ray_examlops_predict_requests_total"

#: Statuses that mean "the caller asked for something the service correctly refused". Never bad
#: events, and never valid events — they belong in neither half of an SLI.
CLIENT_FAULT = frozenset({"invalid", "not_found"})


def emitted_statuses() -> set[str]:
    """Every `status` value the serving code tags the request counter with.

    Derived from the call sites so a new status cannot appear without this guard seeing it. One of
    them is a conditional (`'deadline_exceeded' if budget_limited else 'timeout'`), so the walk
    collects every string constant in the tag value rather than only a bare literal.
    """
    src = (ROOT / "serving/ray_serving/app.py").read_text()
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "attr", None) != "inc":
            continue
        if getattr(getattr(func, "value", None), "attr", None) != "_req_counter":
            continue
        for kw in node.keywords:
            if kw.arg != "tags" or not isinstance(kw.value, ast.Dict):
                continue
            for key, value in zip(kw.value.keys, kw.value.values, strict=False):
                if isinstance(key, ast.Constant) and key.value == "status":
                    found.update(
                        n.value
                        for n in ast.walk(value)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    )
    assert found, "no `_req_counter.inc(tags={'status': …})` call sites found — the sweep is blind"
    return found


def _ratio_expressions() -> list[tuple[str, str]]:
    """(where, expr) for every alert/panel expression that divides the serving counter by itself."""
    out: list[tuple[str, str]] = []
    text = RULES.read_text()
    for match in re.finditer(r"- alert:\s*(\S+)(.*?)(?=\n\s*- alert:|\Z)", text, re.S):
        name, body = match.group(1), match.group(2)
        if SERVING_COUNTER in body and "/" in body:
            out.append((f"alert_rules.yml:{name}", body))
    for file in sorted(DASHBOARDS.glob("*.json")):
        doc = json.loads(file.read_text())

        def walk(panels):
            for panel in panels:
                yield panel
                yield from walk(panel.get("panels", []))

        for panel in walk(doc.get("panels", [])):
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if SERVING_COUNTER in expr and "/" in expr:
                    out.append((f"{file.name}:{panel.get('title')}", expr))
    return out


def test_the_sweep_finds_the_expressions_it_judges():
    """Anti-vacuity: a sweep that matches nothing reports a clean tree forever."""
    found = _ratio_expressions()
    assert len(found) >= 8, f"only {len(found)} ratio expressions found — the sweep is not reading"
    assert any(w.startswith("alert_rules.yml:") for w, _ in found)
    assert any(w.endswith(".json") or ".json:" in w for w, _ in found)


def test_client_faults_are_not_valid_events_in_any_error_rate():
    """The denominator must name its statuses wherever the numerator does."""
    offenders = []
    for where, expr in _ratio_expressions():
        # the denominator is everything after the division
        _, _, denominator = expr.partition("/")
        if SERVING_COUNTER not in denominator:
            continue
        bare = re.findall(rf"{SERVING_COUNTER}(?!\s*\{{)", denominator)
        if bare:
            offenders.append(where)
    assert not offenders, (
        "these divide by the unfiltered serving counter, so client faults (`invalid`, `not_found`) "
        "sit in the denominator and a burst of malformed requests lowers the measured error rate — "
        f"suppressing the very alerts that should fire during an outage: {sorted(set(offenders))}"
    )


def test_no_client_fault_is_counted_as_a_bad_event():
    """The other direction: refusing a malformed request is not a serving failure."""
    offenders = []
    for where, expr in _ratio_expressions():
        numerator, _, _ = expr.partition("/")
        named = {
            v for m in re.findall(r'status\s*=~?\s*"([^"]+)"', numerator) for v in m.split("|")
        }
        if named & CLIENT_FAULT:
            offenders.append(f"{where} counts {sorted(named & CLIENT_FAULT)}")
    assert not offenders, (
        "a request the service correctly refused is not an error it made: " + "; ".join(offenders)
    )


def test_every_status_is_classified():
    """A new status must be placed deliberately, not inherit a default.

    If serving starts emitting a status this guard has never seen, it belongs in exactly one of
    three buckets — good, bad, or client fault — and somebody has to decide which.
    """
    good = {"success"}
    bad = {"error", "timeout", "deadline_exceeded"}
    unclassified = emitted_statuses() - good - bad - CLIENT_FAULT
    assert not unclassified, (
        f"new serving status(es) {sorted(unclassified)} are in no bucket. Decide whether each is a "
        "good event, a bad event, or a client fault, and add it here and to the SLI expressions."
    )


# ── the claim, executed rather than argued ────────────────────────────────────


def evaluate(expr: str, traffic: dict[str, float]) -> float:
    """Evaluate one of these `bad / valid` expressions against a per-status traffic mix.

    A small status-aware evaluator, not a string substitution: each `metric{status=~"a|b"}[w]` term
    is replaced by the summed rate of the statuses it actually selects, and a bare term by the sum
    of everything. That is the only way to *demonstrate* the dilution claim — a substitution that
    labels one term "errors" and the other "total" cannot represent traffic the expression does not
    select, which is precisely what the defect was about.
    """
    threshold_stripped = re.split(r"[<>]", expr)[0]

    def _term(match: re.Match[str]) -> str:
        selector = match.group(0)
        named = re.search(r'status\s*=~?\s*"([^"]+)"', selector)
        statuses = set(named.group(1).split("|")) if named else set(traffic)
        return repr(sum(rate for s, rate in traffic.items() if s in statuses))

    body = re.sub(rf"{SERVING_COUNTER}(?:\{{[^}}]*\}})?\[[0-9a-z]+\]", _term, threshold_stripped)
    body = re.sub(r"sum\(rate\(([0-9.e+-]+)\)\)", r"\1", body)
    assert SERVING_COUNTER not in body and "rate(" not in body, f"unsubstituted: {body}"
    return float(eval(body, {"__builtins__": {}}, {"clamp_min": max}))  # noqa: S307


def _error_rate_expr() -> str:
    text = RULES.read_text()
    match = re.search(r"- alert:\s*RayServeHighErrorRate\b(.*?)(?=\n\s*- alert:)", text, re.S)
    assert match, "RayServeHighErrorRate not found in the rules"
    expr = re.search(r"expr:\s*\|\s*\n(.*?)\n\s+for:", match.group(1), re.S)
    assert expr, "could not read the expression"
    return " ".join(expr.group(1).split())


def test_the_evaluator_reproduces_a_known_rate():
    """Anti-vacuity: prove the evaluator computes what it claims before trusting its verdict."""
    rate = evaluate(_error_rate_expr(), {"success": 95.0, "error": 5.0})
    assert abs(rate - 0.05) < 1e-9, f"expected 5%, got {rate}"


def test_client_faults_cannot_move_the_measured_error_rate():
    """The defect, executed: 900 refused requests must not change a 5% error rate.

    Before 2026-09-14 this expression divided by the whole counter, and the same five server errors
    read as **0.5%** once the callers sent enough junk — below both burn-rate thresholds, so the
    alerts that exist to page during an outage could be held quiet by unrelated traffic.
    """
    expr = _error_rate_expr()
    clean = {"success": 95.0, "error": 5.0}
    flooded = {**clean, "invalid": 800.0, "not_found": 100.0}

    assert abs(evaluate(expr, clean) - 0.05) < 1e-9
    assert abs(evaluate(expr, flooded) - 0.05) < 1e-9, (
        f"client faults moved the error rate to {evaluate(expr, flooded):.4%}; the denominator is "
        "counting requests the service refused"
    )


def test_the_burn_rate_alerts_still_fire_through_a_flood_of_client_faults():
    """The consequence that matters: a real outage must still page while callers send garbage."""
    text = RULES.read_text()
    outage = {"success": 90.0, "error": 10.0, "invalid": 800.0, "not_found": 100.0}  # 10% errors
    for alert, threshold in (("SLOErrorBudgetFastBurn", 0.072), ("SLOErrorBudgetSlowBurn", 0.015)):
        match = re.search(rf"- alert:\s*{alert}\b(.*?)(?=\n\s*- alert:|\Z)", text, re.S)
        assert match, f"{alert} not found"
        expr = re.search(r"expr:\s*\|\s*\n(.*?)\n\s+for:", match.group(1), re.S)
        assert expr, f"could not read {alert}'s expression"
        value = evaluate(" ".join(expr.group(1).split()), outage)
        assert value > threshold, (
            f"{alert} measured {value:.4%} during a 10% outage and would NOT fire — the client "
            "faults are diluting it back under the threshold"
        )


# ── the same rule, for every alert rather than one metric ─────────────────────

#: Alerts whose denominator is deliberately the whole counter — a genuine *share of total*, not an
#: error rate. Each needs a reason here; the list may only shrink.
SHARE_OF_TOTAL_ALERTS: dict[str, str] = {}


def _alert_ratios() -> list[tuple[str, str, str]]:
    """(alert, numerator, denominator) for every alert expression containing a division."""
    out = []
    text = RULES.read_text()
    for match in re.finditer(r"- alert:\s*(\S+)(.*?)(?=\n\s*- alert:|\Z)", text, re.S):
        name, body = match.group(1), match.group(2)
        expr_match = re.search(r"expr:\s*\|?\s*\n?(.*?)\n\s+(?:for|labels):", body, re.S)
        if not expr_match:
            continue
        expr = " ".join(expr_match.group(1).split())
        if "/" not in expr:
            continue
        numerator, _, denominator = expr.partition("/")
        out.append((name, numerator, denominator))
    return out


def test_no_alert_divides_a_filtered_numerator_by_an_unfiltered_counter():
    """The serving fix generalised: the same shape existed on the retrain path.

    Whenever an alert's numerator selects *some* of a counter's label values and its denominator
    takes *all* of them, every value outside the numerator dilutes the rate. `HighRetrainErrorRate`
    divided by the whole counter, so 3 errors out of 5 attempts (60%, firing) became 5.5% once 50
    requests were **throttled** — and throttling is what happens when a system is already in
    trouble, so the dilution is anti-correlated with the alert firing.
    """
    offenders = []
    for name, numerator, denominator in _alert_ratios():
        if name in SHARE_OF_TOTAL_ALERTS:
            continue
        for metric in set(re.findall(r"\b([a-z][a-z0-9_]*(?:_total|_count))\b", denominator)):
            filtered_numerator = re.search(rf"{metric}\s*\{{", numerator)
            bare_denominator = re.search(rf"{metric}(?!\s*\{{)", denominator)
            if filtered_numerator and bare_denominator:
                offenders.append(f"{name} ({metric})")
    assert not offenders, (
        "these select some label values in the numerator and all of them in the denominator, so "
        "unrelated traffic lowers the measured rate and can hold the alert below its threshold: "
        f"{sorted(set(offenders))}"
    )


def test_the_alert_ratio_sweep_reads_real_alerts():
    """Anti-vacuity for the generalised sweep."""
    ratios = _alert_ratios()
    assert len(ratios) >= 6, f"only {len(ratios)} ratio alerts found — the sweep is not reading"
    assert any(n == "HighRetrainErrorRate" for n, _, _ in ratios)


def test_the_share_of_total_exemptions_are_still_needed():
    """An exemption that no longer applies must be deleted, not left to cover a future defect."""
    names = {name for name, _, _ in _alert_ratios()}
    stale = sorted(set(SHARE_OF_TOTAL_ALERTS) - names)
    assert not stale, f"these exempted alerts no longer exist: {stale}"
