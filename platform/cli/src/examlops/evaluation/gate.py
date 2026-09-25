"""C3 — Eval regression testing as a promotion/CI gate (ADR 0008).

Given a per-model gate config ``{suite, baseline_alias, metrics:[{name,min?,max?,max_drop?,higher_is_better?}],
mode}``, compare a candidate version's C2 scores against the baseline alias's scores and
decide pass/fail. ``block`` mode fails the build/promotion; ``warn`` records only. The
report is persisted to ``platform_db.gate_reports``.

**ADR 0111 rides on top of this gate.** If the compared scores came from an LLM judge, the
judge must have passed the MVVP (:mod:`examlops.evaluation.calibration`) or the gate refuses
outright — in ``warn`` mode too. That asymmetry is deliberate: ``warn`` is a statement about
*metric regressions* being advisory, never a licence to let an unmeasured instrument decide
what reaches production.
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
    #: Ceiling, if one was configured. Reported because ``run_eval_gate`` persists ``vars()``
    #: of every verdict — a cap absent from the report is a cap nobody can audit afterwards.
    max: float | None = None
    #: Whether this failure is an **absolute** one — a floor, a ceiling, or a score that is not
    #: there — as opposed to a regression measured against a baseline. Clause 5's aggregate
    #: policy applies only to the latter: a `max_drop` comparison is where sampling noise lives,
    #: while a floor or ceiling is a statement about the candidate alone and no amount of
    #: agreement from other metrics makes an unsafe model safe.
    hard: bool = False


@dataclass
class GateResult:
    passed: bool
    mode: str
    metrics: list[MetricVerdict] = field(default_factory=list)
    judge: str | None = None
    judge_eligible: bool = True
    judge_failures: list[str] = field(default_factory=list)
    calibration_id: str | None = None
    #: Which clause-5 aggregate policy decided this result. On the report so a reader can tell
    #: a pass under ``majority`` from a pass under ``all`` without re-deriving it.
    aggregate: str = "all"

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "aggregate": self.aggregate,
            "metrics": [vars(m) for m in self.metrics],
            "judge": self.judge,
            "judge_eligible": self.judge_eligible,
            "judge_failures": list(self.judge_failures),
            "calibration_id": self.calibration_id,
        }


def evaluate_gate(
    metrics_cfg: list[dict[str, Any]],
    candidate_scores: dict[str, float],
    baseline_scores: dict[str, float],
    *,
    mode: str = "block",
    higher_is_better: bool = True,
    aggregate: str | None = None,
) -> GateResult:
    """Pure gate decision (R4). A metric fails if it regresses beyond ``max_drop``, falls
    below ``min``, or rises above ``max``.

    ``higher_is_better`` sets the gate's default direction; **any metric may override it**
    with its own ``higher_is_better`` key. That override is what lets one gate cover a suite
    whose scores point both ways — the agent suites store ``answer_rate`` next to
    ``unsafe_rate`` and ``latency_p95`` — where a single flag reads one half backwards and a
    safety metric cannot fail. ``max`` is a plain ceiling and ignores the direction entirely.
    """
    verdicts: list[MetricVerdict] = []
    for m in metrics_cfg:
        name = m["name"]
        cand = candidate_scores.get(name)
        base = baseline_scores.get(name)
        min_v = m.get("min")
        max_v = m.get("max")
        max_drop = m.get("max_drop")
        # Per-metric direction, defaulting to the gate's. One direction for the whole gate
        # cannot describe a suite that stores a mix — the agent suites record `answer_rate`
        # (up is better) beside `unsafe_rate` and `latency_p95` (down is better) in one scores
        # dict, and under a single flag one half of that gate is always read backwards.
        rising = bool(m.get("higher_is_better", higher_is_better))
        failed = False
        hard = False
        reasons: list[str] = []

        if cand is None:
            failed = True
            hard = True  # nothing was measured; that is not noise to be outvoted
            reasons.append("candidate score missing")
        else:
            # Floor violation — "floor" in the metric's own direction.
            if min_v is not None:
                below = cand < min_v if rising else cand > min_v
                if below:
                    failed = True
                    hard = True
                    reasons.append(f"floor {min_v} violated (got {cand})")
            # Ceiling violation. Direction-independent on purpose: a latency budget or an
            # unsafe-rate cap means the same thing however the gate leans, and expressing one
            # as a floor read backwards is how a cap becomes silent.
            if max_v is not None and cand > max_v:
                failed = True
                hard = True
                reasons.append(f"ceiling {max_v} exceeded (got {cand})")
            # Regression vs baseline.
            if max_drop is not None and base is not None:
                drop = (base - cand) if rising else (cand - base)
                if drop > max_drop:
                    failed = True
                    reasons.append(f"regressed {drop:.4f} > max_drop {max_drop}")

        delta = (cand - base) if (cand is not None and base is not None) else None
        verdicts.append(
            MetricVerdict(
                name, cand, base, delta, min_v, max_drop, failed, "; ".join(reasons), max_v, hard
            )
        )

    blocked = _aggregate_blocks(verdicts, aggregate)
    # In warn mode the gate always "passes" (never blocks) but records the failures.
    passed = (not blocked) if mode == "block" else True
    return GateResult(
        passed=passed, mode=mode, metrics=verdicts, aggregate=_norm_aggregate(aggregate)
    )


#: Clause 5's aggregate policies. ``all`` is the default and is byte-identical to the behaviour
#: before the policy existed — **changing the default would silently weaken every gate already
#: configured**, turning a promotion that blocks today into one that passes tomorrow with no
#: config change and no message. Loosening a gate is opt-in, per gate, and recorded in its report.
AGGREGATES = ("all", "majority")


def _norm_aggregate(aggregate: str | None) -> str:
    """Unknown policy ⇒ ``all``. A typo must fail closed, never open."""
    value = (aggregate or "all").strip().lower()
    return value if value in AGGREGATES else "all"


def _aggregate_blocks(verdicts: list[MetricVerdict], aggregate: str | None) -> bool:
    """Whether the configured metrics, taken together, block (clause 5).

    ``all`` — any failing metric blocks.

    ``majority`` — a **regression** failure blocks only when more than half the configured
    metrics regressed, so one noisy metric cannot alone veto a genuine improvement. An
    absolute failure (floor, ceiling, missing score) still blocks on its own: those are
    statements about the candidate itself, not comparisons that carry sampling noise, and a
    safety cap that can be outvoted by unrelated metrics is not a cap.
    """
    if any(v.failed and v.hard for v in verdicts):
        return True
    soft = [v for v in verdicts if v.failed]
    if not soft:
        return False
    if _norm_aggregate(aggregate) == "majority":
        return len(soft) * 2 > len(verdicts)
    return True


def run_eval_gate(
    model: str,
    candidate_version: str,
    *,
    candidate_scores: dict[str, float] | None = None,
    baseline_scores: dict[str, float] | None = None,
    higher_is_better: bool = True,
    persist: bool = True,
    judge: str | None = None,
) -> GateResult | None:
    """Run the configured gate for a model. Returns None if no gate is configured.

    Scores may be supplied directly (tests / on-demand eval) or resolved from the latest
    persisted C2 results for the candidate version and the baseline alias.

    ``higher_is_better`` here is only a **fallback**: a gate that declares its own direction
    wins, and a per-metric ``higher_is_better`` key wins over both.
    """
    from examlops.data.evaluation import get_eval_gate, get_eval_results, record_gate_report

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

    # Direction precedence: per-metric key > the gate's own declared direction > the caller's.
    # The callers derive theirs from the promotion *rule's* operator (`--if-rmse-lt 5.0` →
    # lower-is-better), which is a threshold on one MLflow metric and says nothing about the
    # direction of the suite metrics this gate names. A gate that declares its own direction is
    # judged the same way whoever runs it; one that does not keeps the old behaviour exactly.
    declared = gate.get("higher_is_better")
    result = evaluate_gate(
        gate["metrics"],
        candidate_scores,
        baseline_scores,
        mode=gate["mode"],
        higher_is_better=higher_is_better if declared is None else bool(declared),
        aggregate=gate.get("aggregate"),
    )
    if judge is None:
        judge = judge_for_results(model, gate["suite"], candidate_version)
    apply_judge_eligibility(result, judge)
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


def promotion_refusal(
    names: list[str],
    candidate_version: str,
    *,
    higher_is_better: bool = True,
    actor: str = "pipeline",
    source: str = "pipeline",
) -> str | None:
    """Why the eval gate refuses to let ``candidate_version`` move past the candidate stage.

    For callers that move aliases on their own road — the training flow's lifecycle promotion
    (``pipelines.pipeline_generator.promote_task``). ``names`` are the keys the model may be gated
    under (registry name, MLflow name); the first with a configured gate is used.

    Returns None when no gate is configured, or when it passes (a ``warn``-mode gate always
    passes). A configured gate that **could not run** refuses — ADR 0008 clause 2's rule: a gate
    that could not run is reported, never passed over. Every refusal is audited
    (``promotion_blocked_by_gate`` / ``promotion_gate_error``).
    """
    from examlops.data.evaluation import get_eval_gate

    target = next((n for n in names if n), "?")
    # ADR 0016 decision 3: a quantized version must clear the mandatory quality-retention gate
    # against its base — checked first, because "no gate configured" returns early below and
    # must not wave a quantization through. A non-quantized version makes this a no-op.
    try:
        from examlops.engines.quality import quantization_quality_gate

        # Judge under the name the gate is configured for: ``names`` may hold the registry name
        # and the MLflow name, and only one of them may carry the gate. The first name used
        # blindly would refuse a correctly gated quantization as "no gate configured".
        qkey = next((n for n in names if n and get_eval_gate(n) is not None), target)
        quality = quantization_quality_gate(qkey, str(candidate_version), actor=actor)
    except Exception as exc:  # noqa: BLE001
        reason = f"quantization quality gate could not run ({exc})"
        _audit_refusal(source, actor, "promotion_gate_error", target, candidate_version, reason, [])
        return reason
    if quality is not None and not quality.passed:
        _audit_refusal(
            source,
            actor,
            "promotion_blocked_by_quantization_gate",
            target,
            candidate_version,
            quality.reason,
            quality.failing,
        )
        return quality.reason
    try:
        key = next((n for n in names if n and get_eval_gate(n) is not None), None)
        if key is None:
            return None
        target = key
        result = run_eval_gate(key, str(candidate_version), higher_is_better=higher_is_better)
    except Exception as exc:  # noqa: BLE001
        reason = f"eval gate could not run ({exc})"
        _audit_refusal(source, actor, "promotion_gate_error", target, candidate_version, reason, [])
        return reason
    if result is None or result.passed:
        return None
    failing = [m.name for m in result.metrics if m.failed] or list(result.judge_failures)
    reason = f"eval gate FAILED: {', '.join(failing) or 'gate failed'}"
    _audit_refusal(
        source, actor, "promotion_blocked_by_gate", target, candidate_version, reason, failing
    )
    return reason


def _audit_refusal(
    source: str,
    actor: str,
    action: str,
    target: str,
    version: str,
    reason: str,
    failing: list[str],
) -> None:
    # The refusal stands whether or not it is audited — but an Art. 12 required event that never
    # lands is invisible to both the hash chain and the coverage report, so the loss is logged and
    # counted rather than swallowed. See `audit.audit_best_effort`.
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        source,
        actor,
        action,
        target,
        {"version": str(version), "reason": reason, "failing_metrics": failing},
    )


# ── ADR 0111 — no uncalibrated judge may gate ─────────────────────────────────


def judge_for_results(model: str, suite: str, version: str | None = None) -> str | None:
    """The judge that produced this model's suite scores, or None if none did.

    A suite of purely deterministic evaluators has no judge, and nothing to calibrate — the
    ADR constrains judged evaluation, not exact-match.
    """
    from examlops.data.evaluation import get_eval_results

    for row in get_eval_results(model, suite):
        if version is not None and str(row.get("model_version")) != str(version):
            continue
        if row.get("judge_model"):
            return str(row["judge_model"])
    return None


def apply_judge_eligibility(result: GateResult, judge: str | None) -> GateResult:
    """Refuse the gate when a judge decided it and that judge is not MVVP-eligible.

    Mutates and returns ``result`` so callers keep one object. A refusal appends a
    ``judge_calibration`` verdict, so every existing consumer that lists failing metric names
    reports the real reason without knowing anything about ADR 0111.
    """
    if not judge:
        return result

    from examlops.data.evaluation import get_judge_calibration
    from examlops.evaluation.calibration import is_gate_eligible

    eligible, failures = is_gate_eligible(judge)
    result.judge = judge
    result.judge_eligible = eligible
    result.judge_failures = failures
    row = get_judge_calibration(judge)
    result.calibration_id = (row or {}).get("calibration_id")
    if not eligible:
        result.passed = False  # in warn mode too — see the module docstring
        result.metrics.append(
            MetricVerdict(
                name="judge_calibration",
                candidate=None,
                baseline=None,
                delta=None,
                min=None,
                max_drop=None,
                failed=True,
                reason=f"judge {judge!r} is not gate-eligible: {', '.join(failures)}",
            )
        )
    return result


def judge_eligibility_for_model(model: str) -> tuple[bool, list[str], str | None]:
    """``(eligible, failures, judge)`` for whichever judge last scored ``model``.

    Used by callers that promote *without* going through :func:`run_eval_gate` — the autopilot's
    closed loop — so an unmeasured judge cannot reach production by taking the other road.
    """
    from examlops.data.evaluation import get_eval_gate

    gate = get_eval_gate(model)
    if gate is None:
        return (True, [], None)
    judge = judge_for_results(model, gate["suite"])
    if not judge:
        return (True, [], None)
    from examlops.evaluation.calibration import is_gate_eligible

    eligible, failures = is_gate_eligible(judge)
    return (eligible, failures, judge)
