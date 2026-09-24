"""One evaluation-evidence check, shared by every registry that gates a Production alias.

ADR 0146 (agent versions) and ADR 0159 (GenAI applications) both promise the *same* gate: the
platform's own ``exa eval gate`` config and results, keyed by a registered-model name, with the
judge-calibration rule of ADR 0111 (``no uncalibrated judge may gate``) applied through the same
code path a predictive model takes. Two registries asking that question two ways would be two
gates, and the weaker one would decide. So the question is asked here, once.

**Absence is not evidence.** No configured gate, a gate in ``warn`` mode, a metric with no score
for this version, a declared suite with no results - each refuses, with the reason named.
"""

from __future__ import annotations

from typing import Any

__all__ = ["evaluation_evidence"]


def evaluation_evidence(
    key: str, version_id: str, declared_suites: list[str]
) -> tuple[list[str], dict[str, Any]]:
    """``(reasons, evidence)``; empty ``reasons`` means the evaluation evidence is sufficient.

    ``key`` is the registered-model name the results are recorded under (``agent-<name>``,
    ``genai-app-<name>``), ``version_id`` the artifact being promoted, and ``declared_suites`` the
    bare suite names its manifest declares (``[]`` when it declares none).
    """
    from examlops.data.evaluation import get_eval_gate, get_eval_results
    from examlops.evaluation.calibration import is_gate_eligible
    from examlops.evaluation.gate import judge_for_results, run_eval_gate

    gate = get_eval_gate(key)
    if gate is None:
        return [
            f"no evaluation evidence: no eval gate is configured for {key} "
            f"(exa eval gate set {key} --suite <suite> ...)"
        ], {}
    reasons: list[str] = []
    if gate["mode"] != "block":
        reasons.append(f"the eval gate for {key} is in {gate['mode']} mode; promotion needs block")

    def scored(suite: str) -> dict[str, float]:
        return {
            r["metric"]: r["score"]
            for r in get_eval_results(key, suite)
            if str(r["model_version"]) == version_id
        }

    declared = list(declared_suites)
    if declared and gate["suite"] not in declared:
        reasons.append(f"gate suite {gate['suite']!r} is not among the declared suites {declared}")
    suites = sorted(set(declared) | {gate["suite"]})
    evidence: dict[str, Any] = {"model": key, "gate_suite": gate["suite"], "suites": suites}
    for suite in suites:
        got = scored(suite)
        if not got:
            reasons.append(
                f"no evaluation evidence: suite {suite!r} has no results for {version_id}"
            )
            continue
        judge = judge_for_results(key, suite, version_id)
        if judge:
            ok, fails = is_gate_eligible(judge)
            if not ok:
                reasons.append(f"judge {judge!r} is not gate-eligible: {', '.join(fails)}")
    missing = [m["name"] for m in gate["metrics"] if m["name"] not in scored(gate["suite"])]
    if missing:
        reasons.append(f"no score for gate metric(s) {missing} on {version_id}")
    if reasons:
        return reasons, evidence
    result = run_eval_gate(key, version_id, persist=True)
    if result is None:  # cannot happen: the gate exists; kept so absence can never pass
        return [f"no evaluation evidence: the gate for {key} did not run"], evidence
    evidence.update(
        gate_passed=result.passed, judge=result.judge, calibration_id=result.calibration_id
    )
    if not result.passed:
        failing = [m.name for m in result.metrics if m.failed]
        return [f"the eval gate failed on: {', '.join(failing) or 'judge calibration'}"], evidence
    return [], evidence
