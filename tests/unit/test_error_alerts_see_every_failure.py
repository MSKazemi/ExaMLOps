"""An alert that selects the wrong label values cannot fire for the failure it is named after.

`ray_examlops_predict_requests_total` carries a `status` label, and the serving code emits five
values for it. Four alerts — `RayServeHighErrorRate`, `RayServeHighErrorRateCritical` and both SLO
error-budget burn alerts — selected only `status="error"`, so `status="timeout"` counted as neither
an error nor a burn. A timeout is `FuturesTimeoutError` returned as HTTP 504, raised by the very
guard the code added so "a hung model can't pin the replica worker": a replica hanging on every
request would have shown an error rate of 0/N and burned error budget at 0×, with all four alerts
silent. The same file already used the right idiom for reloads (`status!="success"`), which is how
you can tell `status="error"` was never a decision.

Not every status belongs in the error rate. `invalid` is a 422 the code deliberately keeps out —
its comment says so — and `not_found` is a 404 for a model the caller asked for by name. So the
rule is not "count everything"; it is that **every value the code emits must be classified**. A new
status added to the serving path lands here as a failure until someone decides which side it is on.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SERVING = _ROOT / "serving" / "ray_serving" / "app.py"
_RULES = _ROOT / "platform" / "infra" / "docker-compose" / "alert_rules.yml"

_METRIC = "ray_examlops_predict_requests_total"

# Statuses that are deliberately NOT server-side failures. Each needs a reason, because the whole
# point of this guard is that leaving a status unclassified is what went wrong last time.
_NOT_A_SERVER_FAILURE = {
    "success": "the request worked",
    "invalid": "422 — a malformed feature vector is the caller's error, and the serving code "
    "comments that it deliberately keeps this out of the error rate",
    "not_found": "404 — the caller named a model or alias that does not exist",
}

# The alerts whose whole subject is the server-side failure rate.
_ERROR_RATE_ALERTS = {
    "RayServeHighErrorRate",
    "RayServeHighErrorRateCritical",
    "SLOErrorBudgetFastBurn",
    "SLOErrorBudgetSlowBurn",
}


def _emitted_statuses() -> set[str]:
    """Every `status` value the serving path records on the request counter."""
    tree = ast.parse(_SERVING.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "attr", None) != "inc":
            continue
        if "_req_counter" not in ast.unparse(func):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for d in ast.walk(arg):
                if not isinstance(d, ast.Dict):
                    continue
                for k, v in zip(d.keys, d.values, strict=False):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "status"
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                    ):
                        found.add(v.value)
    return found


def _selected_statuses() -> dict[str, set[str]]:
    """The status values each error-rate alert's selector actually matches."""
    rules = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    selector = re.compile(re.escape(_METRIC) + r"\{status(=~|=)\"([^\"]*)\"\}")
    for group in rules.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert")
            if name not in _ERROR_RATE_ALERTS:
                continue
            # The NUMERATOR only. These expressions are `bad / valid > threshold`, and since
            # 2026-09-14 the denominator names its statuses too (so client faults cannot dilute
            # the rate — `test_sli_counts_only_valid_events.py`). Reading the whole expression
            # made `success` and `not_found` look like things the alert counts as errors, which
            # is the opposite of what both halves of this file assert.
            numerator = rule.get("expr", "").partition("/")[0]
            values: set[str] = set()
            for op, body in selector.findall(numerator):
                values |= set(body.split("|")) if op == "=~" else {body}
            out[name] = values
    return out


def test_the_scan_found_the_serving_path_and_the_alerts():
    """Both halves must be real, or every comparison below is between two empty sets."""
    assert _SERVING.exists() and _RULES.exists()
    emitted = _emitted_statuses()
    assert len(emitted) >= 4, f"only found {emitted} on the request counter; the scan is stale"
    selected = _selected_statuses()
    assert set(selected) == _ERROR_RATE_ALERTS, (
        f"expected selectors for {sorted(_ERROR_RATE_ALERTS)}, found {sorted(selected)}"
    )


def test_every_emitted_status_is_classified():
    unclassified = sorted(
        s
        for s in _emitted_statuses()
        if s not in _NOT_A_SERVER_FAILURE
        and not all(s in vals for vals in _selected_statuses().values())
    )
    assert not unclassified, (
        "these `status` values are recorded by the serving path but are neither selected by the "
        f"error-rate alerts nor listed as not-a-server-failure: {unclassified}. An unclassified "
        "status is invisible to alerting — decide which side it is on."
    )


def test_a_timeout_counts_as_a_server_failure():
    """The specific regression: a hung model returning 504s must burn error budget."""
    for alert, values in _selected_statuses().items():
        assert "timeout" in values, f"{alert} does not count timeouts as failures"


def test_a_request_that_ran_out_of_deadline_counts_as_a_server_failure():
    """A 504 the replica returns because a queued request's budget expired is the overload
    signal itself (plan P4.6) — it must burn error budget, not vanish from it."""
    for alert, values in _selected_statuses().items():
        assert "deadline_exceeded" in values, f"{alert} does not count expired deadlines"


#: Statuses that mean the caller was wrong, not the service. Shared definition with
#: `test_sli_counts_only_valid_events.py`, which keeps them out of both halves of the SLI.
_CLIENT_FAULT = frozenset({"invalid", "not_found"})

_DASHBOARDS = _ROOT / "platform" / "infra" / "docker-compose" / "grafana" / "provisioning"


def test_error_rate_panels_count_what_the_alerts_count():
    """A Grafana SLO panel showing 100 % compliance while the burn alert fires is the same defect
    seen from the other side: every panel that selects failures selects the alerts' set."""
    alerted = set.intersection(*_selected_statuses().values())
    selector = re.compile(re.escape(_METRIC) + r'\{status(=~|=)\\?"([^"\\]*)\\?"\}')
    seen = 0
    for board in (_DASHBOARDS / "dashboards").glob("*.json"):
        for op, body in selector.findall(board.read_text(encoding="utf-8")):
            values = set(body.split("|")) if op == "=~" else {body}
            # A selector naming `success` is not a failure selector: it is either the success
            # series itself or, since 2026-09-14, an SLI *denominator* naming the valid events
            # (`success|error|timeout|deadline_exceeded`) so that client faults cannot dilute the
            # rate. Comparing either against the alerts' failure set is a category error.
            if "success" in values:
                continue
            # Nor is a selector made up ENTIRELY of client faults: a panel showing requests the
            # service correctly refused is not claiming to show server failures. `<=` and not an
            # intersection on purpose — a selector that MIXES them (`error|invalid`) is exactly the
            # defect `test_client_errors_stay_out_of_the_server_error_rate` exists for, and must
            # still be compared.
            if values <= _CLIENT_FAULT:
                continue
            seen += 1
            assert values == alerted, (
                f"{board.name} selects {sorted(values)}, alerts {sorted(alerted)}"
            )
    assert seen >= 5, "the dashboard scan found no failure panels; it is stale"


def test_client_errors_stay_out_of_the_server_error_rate():
    """The other direction — widening the selector must not sweep 4xx in."""
    for alert, values in _selected_statuses().items():
        assert "invalid" not in values, f"{alert} counts a 422 against the server"
        assert "success" not in values, f"{alert} counts successes as errors"
