"""C3 — Eval regression testing as a promotion/CI gate (ADR 0008).

Given a per-model gate config ``{suite, baseline_alias, metrics:[{name,min?,max_drop?}],
mode}``, compare a candidate version's C2 scores against the baseline alias's scores and
decide pass/fail. ``block`` mode fails the build/promotion; ``warn`` records only. The
report is persisted to ``platform_db.gate_reports``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricVerdict:
    name: str
    candidate: float | None
    baseline: float | None
    delta: float | None
    min: float | None
    max_drop: float | None
    failed: bool
    reason: str = ""


@dataclass
class GateResult:
    passed: bool
    mode: str
    metrics: list[MetricVerdict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "metrics": [vars(m) for m in self.metrics],
        }


def evaluate_gate(
    metrics_cfg: list[dict[str, Any]],
    candidate_scores: dict[str, float],
    baseline_scores: dict[str, float],
    *,
    mode: str = "block",
    higher_is_better: bool = True,
) -> GateResult:
    """Pure gate decision (R4). A metric fails if it regresses beyond ``max_drop`` or
    violates ``min``. ``higher_is_better`` flips the regression direction for error metrics.
    """
    verdicts: list[MetricVerdict] = []
    for m in metrics_cfg:
        name = m["name"]
        cand = candidate_scores.get(name)
        base = baseline_scores.get(name)
        min_v = m.get("min")
        max_drop = m.get("max_drop")
        failed = False
        reasons: list[str] = []

        if cand is None:
            failed = True
            reasons.append("candidate score missing")
        else:
            # Floor violation.
            if min_v is not None:
                below = cand < min_v if higher_is_better else cand > min_v
                if below:
                    failed = True
                    reasons.append(f"floor {min_v} violated (got {cand})")
            # Regression vs baseline.
            if max_drop is not None and base is not None:
                drop = (base - cand) if higher_is_better else (cand - base)
                if drop > max_drop:
                    failed = True
                    reasons.append(f"regressed {drop:.4f} > max_drop {max_drop}")

        delta = (cand - base) if (cand is not None and base is not None) else None
        verdicts.append(
            MetricVerdict(name, cand, base, delta, min_v, max_drop, failed, "; ".join(reasons))
        )

    any_failed = any(v.failed for v in verdicts)
    # In warn mode the gate always "passes" (never blocks) but records the failures.
    passed = (not any_failed) if mode == "block" else True
    return GateResult(passed=passed, mode=mode, metrics=verdicts)


def run_eval_gate(
    model: str,
    candidate_version: str,
    *,
    candidate_scores: dict[str, float] | None = None,
    baseline_scores: dict[str, float] | None = None,
    higher_is_better: bool = True,
    persist: bool = True,
) -> GateResult | None:
    """Run the configured gate for a model. Returns None if no gate is configured.

    Scores may be supplied directly (tests / on-demand eval) or resolved from the latest
    persisted C2 results for the candidate version and the baseline alias.
    """
    from examlops.platform_db import get_eval_gate, get_eval_results, record_gate_report

    gate = get_eval_gate(model)
    if gate is None:
        return None

    if candidate_scores is None:
        rows = get_eval_results(model, gate["suite"])
        candidate_scores = {
            r["metric"]: r["score"]
            for r in rows
            if str(r["model_version"]) == str(candidate_version)
        }
    if baseline_scores is None:
        rows = get_eval_results(model, gate["suite"], alias=gate["baseline_alias"])
        baseline_scores = {r["metric"]: r["score"] for r in rows}

    result = evaluate_gate(
        gate["metrics"],
        candidate_scores,
        baseline_scores,
        mode=gate["mode"],
        higher_is_better=higher_is_better,
    )
    if persist:
        record_gate_report(
            model,
            result.passed,
            result.mode,
            result.as_dict(),
            candidate=candidate_version,
            baseline=gate["baseline_alias"],
        )
    return result
