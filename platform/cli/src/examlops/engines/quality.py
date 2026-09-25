"""ADR 0016 decision 3 — the **mandatory** C3 quality-retention gate for a quantized version.

``quantize_model()`` registers ``<base>-<method>`` signed and BOM'd, and ADR 0117's portability
gate compares its *numerics* against the base. Neither asks the question decision 3 names: **did
the quantized model keep its quality?** That is a C3 eval-gate question (ADR 0008), and before this
module a quantized version met the C3 gate only if the model happened to have one configured —
and then it was scored against the *baseline alias*, not against the version it was derived from.

This gate differs from the general C3 gate in three deliberate ways:

1. **It is mandatory.** A quantized version with no configured C3 gate is *refused*, not waved
   through: "no gate" means quality retention was never measured, and an unmeasured quantization
   is exactly the risk the ADR's consequences section says the gate mitigates.
2. **The baseline is the base version.** ``17-awq`` is compared against ``17``'s scores on the
   same suite — quality *retention* — not against whatever the baseline alias currently points at.
3. **It always blocks.** A ``warn``-mode gate is advisory for ordinary regressions; it does not
   license promoting a quantization whose quality loss was measured and found too large, and a
   ``majority`` aggregate does not let one metric's loss be outvoted — every metric must retain.
   Every gate metric without its own ``max_drop`` is given ``EXAMLOPS_QUANTIZATION_MAX_DROP``
   (default ``0.01``) so every metric is compared against the base, not only those that declared
   a drop.

Missing scores on either side refuse too (absent ≠ pass) — including a single gate metric the base
was never scored on. ADR 0111's judge-calibration refusal
applies unchanged. Every evaluation is persisted to ``gate_reports`` and audited.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_QUANTIZATION_MAX_DROP",
    "QualityGateResult",
    "quantization_quality_gate",
]

DEFAULT_QUANTIZATION_MAX_DROP = 0.01


@dataclass
class QualityGateResult:
    passed: bool
    reason: str
    model: str
    version: str
    base_version: str
    method: str
    suite: str | None = None
    failing: list[str] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "model": self.model,
            "version": self.version,
            "base_version": self.base_version,
            "method": self.method,
            "suite": self.suite,
            "failing": list(self.failing),
            "report": self.report,
        }


def _default_max_drop() -> float:
    try:
        value = float(
            os.getenv("EXAMLOPS_QUANTIZATION_MAX_DROP", str(DEFAULT_QUANTIZATION_MAX_DROP))
        )
    except ValueError:
        return DEFAULT_QUANTIZATION_MAX_DROP
    # A negative drop would demand the quantized model *beat* its base; fail closed to default.
    return value if value >= 0 else DEFAULT_QUANTIZATION_MAX_DROP


def _scores(model: str, suite: str, version: str) -> dict[str, float]:
    """Newest score per metric for ``model@version`` (version filtered in SQL, not in Python)."""
    from examlops.data.evaluation import get_version_scores

    return get_version_scores(model, suite, version)


def quantization_quality_gate(
    model: str,
    version: str,
    *,
    candidate_scores: dict[str, float] | None = None,
    base_scores: dict[str, float] | None = None,
    persist: bool = True,
    actor: str | None = None,
) -> QualityGateResult | None:
    """Judge a quantized ``model@version`` against its base version. ``None`` ⇒ not quantized.

    Scores are read from the persisted C2 results (``exa eval run`` for both versions) unless
    supplied directly.
    """
    from examlops.data.evaluation import get_eval_gate
    from examlops.evaluation.gate import (
        apply_judge_eligibility,
        evaluate_gate,
        judge_for_results,
    )
    from examlops.parity import target_change_for_version

    method = target_change_for_version(str(version))
    if method is None:
        return None
    base = str(version)[: -(len(method) + 1)]

    def _finish(result: QualityGateResult) -> QualityGateResult:
        if persist:
            _persist(result, actor)
        return result

    gate = get_eval_gate(model)
    if gate is None:
        return _finish(
            QualityGateResult(
                passed=False,
                reason=(
                    f"no C3 eval gate is configured for {model}; a quantized version cannot be "
                    "promoted without one (ADR 0016 decision 3) — configure it with "
                    "`exa eval gate set`"
                ),
                model=model,
                version=str(version),
                base_version=base,
                method=method,
            )
        )
    suite = str(gate["suite"])
    cand = candidate_scores if candidate_scores is not None else _scores(model, suite, version)
    basescores = base_scores if base_scores is not None else _scores(model, suite, base)
    if not basescores:
        return _finish(
            QualityGateResult(
                passed=False,
                reason=(
                    f"base version {base} has no scores on suite {suite!r} — quality retention "
                    f"cannot be measured (run `exa eval run` for {model} v{base})"
                ),
                model=model,
                version=str(version),
                base_version=base,
                method=method,
                suite=suite,
            )
        )

    # Retention is measured per metric. A gate metric the base was never scored on has nothing to
    # be retained *from*: without this, ``evaluate_gate`` skips its ``max_drop`` comparison (no
    # baseline) and the metric passes unmeasured — absent would be a pass.
    unmeasured = sorted(str(m["name"]) for m in gate["metrics"] if m["name"] not in basescores)
    if unmeasured:
        return _finish(
            QualityGateResult(
                passed=False,
                reason=(
                    f"base version {base} has no score for {', '.join(unmeasured)} on suite "
                    f"{suite!r} — quality retention cannot be measured"
                ),
                model=model,
                version=str(version),
                base_version=base,
                method=method,
                suite=suite,
                failing=unmeasured,
            )
        )

    default_drop = _default_max_drop()
    metrics_cfg = [
        dict(m) if m.get("max_drop") is not None else {**m, "max_drop": default_drop}
        for m in gate["metrics"]
    ]
    declared = gate.get("higher_is_better")
    result = evaluate_gate(
        metrics_cfg,
        cand,
        basescores,
        mode="block",  # decision 3: always blocks, whatever the gate's own mode
        higher_is_better=True if declared is None else bool(declared),
        # Every metric must retain quality. A gate's ``majority`` policy lets one noisy regression
        # be outvoted for an ordinary retrain; a quantization's measured loss is not noise to be
        # outvoted by metrics that happened to survive it.
        aggregate="all",
    )
    apply_judge_eligibility(result, judge_for_results(model, suite, str(version)))
    failing = [m.name for m in result.metrics if m.failed] or list(result.judge_failures)
    if result.passed:
        reason = f"quality retained vs v{base} on suite {suite!r}"
    else:
        reason = f"quality-retention gate FAILED vs v{base}: {', '.join(failing) or 'gate failed'}"
    return _finish(
        QualityGateResult(
            passed=result.passed,
            reason=reason,
            model=model,
            version=str(version),
            base_version=base,
            method=method,
            suite=suite,
            failing=failing,
            report=result.as_dict(),
        )
    )


def _persist(result: QualityGateResult, actor: str | None) -> None:
    """``gate_reports`` row + audit event. The verdict stands even if recording fails."""
    try:
        from examlops.data.evaluation import record_gate_report

        record_gate_report(
            result.model,
            result.passed,
            "block",
            {"kind": "quantization_quality", **result.as_dict()},
            candidate=result.version,
            baseline=f"version:{result.base_version}",
        )
    except Exception:  # noqa: BLE001 - the verdict stands; the lost record must be visible
        log.warning(
            "quantization gate report for %s@%s not recorded",
            result.model,
            result.version,
            exc_info=True,
        )
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        "exa-engines",
        actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown",
        "quantization_quality_gate",
        f"{result.model}@{result.version}",
        {
            "passed": result.passed,
            "base_version": result.base_version,
            "method": result.method,
            "suite": result.suite,
            "failing": result.failing,
            "reason": result.reason,
        },
    )
