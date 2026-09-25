"""C2 — Continuous evaluation harness + LLM-as-judge (ADR 0007).

Engine-agnostic eval *suites* of *evaluators* (deterministic + LLM-as-judge) runnable
against A1-versioned datasets or a traffic sample, persisting scored results to
``platform_db.eval_suite_results``. The judge is any callable (wired to the B2 gateway in
production; a mock in tests) run at temperature 0 with its model + prompt version recorded.

The gate that *consumes* these scores is C3 (:mod:`examlops.evaluation.gate`).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Protocol, runtime_checkable


@dataclass
class EvalItem:
    """A single eval example: model output + optional reference/context."""

    output: str
    reference: str | None = None
    prompt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Retrieved context passages, for retrieval metrics (DeepEval faithfulness/contextual_*).
    contexts: list[str] | None = None
    #: The ``request_hash`` the serving path recorded, for an item pulled from live traffic
    #: (:mod:`examlops.evaluation.traffic`). ``None`` derives one from the prompt, as before.
    recorded_hash: str | None = None

    @property
    def request_hash(self) -> str:
        if self.recorded_hash:
            return self.recorded_hash
        basis = self.prompt or self.output
        return hashlib.sha256(basis.encode()).hexdigest()


@dataclass
class Score:
    metric: str
    score: float  # normalized to [0,1] for judges; numeric for deterministic metrics
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Evaluator(Protocol):
    metric: str

    def score(self, item: EvalItem) -> Score: ...


# ── Deterministic evaluators (R1) ─────────────────────────────────────────────


@dataclass
class ExactMatch:
    metric: str = "exact_match"

    def score(self, item: EvalItem) -> Score:
        hit = 1.0 if item.reference is not None and item.output == item.reference else 0.0
        return Score(self.metric, hit)


@dataclass
class Regex:
    pattern: str
    metric: str = "regex_match"

    def score(self, item: EvalItem) -> Score:
        hit = 1.0 if re.search(self.pattern, item.output) else 0.0
        return Score(self.metric, hit, {"pattern": self.pattern})


@dataclass
class NumericTolerance:
    tolerance: float = 1e-6
    metric: str = "numeric_match"

    def score(self, item: EvalItem) -> Score:
        try:
            got, ref = float(item.output), float(item.reference)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return Score(self.metric, 0.0, {"error": "non-numeric"})
        hit = 1.0 if abs(got - ref) <= self.tolerance else 0.0
        return Score(self.metric, hit, {"delta": abs(got - ref)})


@dataclass
class JSONValid:
    metric: str = "json_valid"

    def score(self, item: EvalItem) -> Score:
        try:
            json.loads(item.output)
            return Score(self.metric, 1.0)
        except (ValueError, TypeError):
            return Score(self.metric, 0.0)


# ── LLM-as-judge (R2, R10) ────────────────────────────────────────────────────


class JudgeTemperatureError(ValueError):
    """A judge was configured, or would be called, at a temperature other than 0 (decision 4)."""


#: ADR 0007 decision 4: judges run at temperature 0. Not a default — the only accepted value.
JUDGE_TEMPERATURE = 0.0


def judge_accepts_temperature(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` takes a ``temperature`` keyword, i.e. whether the harness can *set* it."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "temperature" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def invoke_judge(fn: Callable[..., Any], prompt: str) -> tuple[Any, bool]:
    """Call a judge seam at temperature 0; return ``(raw, enforced)``.

    ``enforced`` is True when the harness itself passed ``temperature=0`` — the gateway judge
    (:func:`examlops.evaluation.judges.gateway_judge`) and any callable taking the keyword. A bare
    ``fn(prompt)`` callable cannot be told its temperature, so the harness refuses one that
    *declares* a non-zero ``temperature`` attribute and records ``enforced=False`` for the rest:
    a convention nobody checked is reported as exactly that, never as a guarantee.
    """
    if judge_accepts_temperature(fn):
        return fn(prompt, temperature=JUDGE_TEMPERATURE), True
    declared = getattr(fn, "temperature", None)
    if declared is not None and float(declared) != JUDGE_TEMPERATURE:
        raise JudgeTemperatureError(
            f"judge callable declares temperature={declared}; ADR 0007 requires 0"
        )
    return fn(prompt), False


@dataclass
class LLMJudge:
    """Score outputs against a rubric via a judge callable, at temperature 0.

    ``judge_fn(prompt[, temperature=0.0]) -> float in [0,1]`` is the seam wired to the B2
    gateway in production (:func:`examlops.evaluation.judges.gateway_judge`) and mocked in
    tests. The judge model + prompt version are recorded per result for governance (R10), and
    so is whether temperature 0 was *enforced* by the harness or only assumed (decision 4).
    ``temperature`` exists to be recorded and checked: any value other than 0 is refused.
    """

    judge_fn: Callable[..., float]
    rubric: str
    judge_model: str = "judge"
    prompt_version: str = "v1"
    metric: str = "judge_score"
    temperature: float = JUDGE_TEMPERATURE

    def __post_init__(self) -> None:
        if float(self.temperature) != JUDGE_TEMPERATURE:
            raise JudgeTemperatureError(
                f"LLMJudge temperature={self.temperature}; ADR 0007 decision 4 requires 0"
            )

    def score(self, item: EvalItem) -> Score:
        prompt = (
            f"{self.rubric}\n\n[OUTPUT]\n{item.output}\n"
            f"[REFERENCE]\n{item.reference or ''}\nScore 0..1:"
        )
        raw, enforced = invoke_judge(self.judge_fn, prompt)
        value = max(0.0, min(1.0, float(raw)))  # clamp to [0,1]
        return Score(
            self.metric,
            value,
            {
                "judge_model": self.judge_model,
                "prompt_version": self.prompt_version,
                "temperature": JUDGE_TEMPERATURE,
                "temperature_enforced": enforced,
            },
        )


# ── Suite ─────────────────────────────────────────────────────────────────────


@dataclass
class SuiteResult:
    suite: str
    scores: dict[str, float]  # metric → mean score across items
    sample_size: int
    judge: dict[str, str] | None = None
    #: Items each metric was actually computed over. Equal to ``sample_size`` unless the suite
    #: skipped items (no reference for a reference metric) or an evaluator raised on some.
    counts: dict[str, int] = field(default_factory=dict)
    #: Per-metric evaluator failures when the suite tolerates them (online eval).
    errors: dict[str, int] = field(default_factory=dict)
    #: Metrics carrying a unit (MAE, latency) rather than a k/n proportion — no Wilson interval.
    non_proportion: set[str] = field(default_factory=set)


def _metric_name(ev: Any) -> str:
    return str(getattr(ev, "metric", type(ev).__name__))


@dataclass
class Suite:
    """A named set of evaluators.

    ``skip_missing_reference`` makes an evaluator that declares ``requires_reference = True``
    skip an item that has no reference instead of scoring it 0 — live traffic rarely has one,
    and an unlabelled request is not a wrong answer. ``tolerate_errors`` counts an evaluator
    that raises on an item (a judge that answered no number) instead of aborting the run. Both
    default off, so an ad-hoc ``exa eval run`` behaves exactly as it always has.
    """

    name: str
    evaluators: list[Any]
    skip_missing_reference: bool = False
    tolerate_errors: bool = False

    def run(self, items: list[EvalItem]) -> SuiteResult:
        by_metric: dict[str, list[float]] = {}
        errors: dict[str, int] = {}
        judge: dict[str, str] | None = None
        non_proportion = {
            _metric_name(ev) for ev in self.evaluators if getattr(ev, "proportion", True) is False
        }
        for item in items:
            for ev in self.evaluators:
                if (
                    self.skip_missing_reference
                    and getattr(ev, "requires_reference", False)
                    and item.reference is None
                ):
                    continue
                try:
                    s = ev.score(item)
                except JudgeTemperatureError:
                    raise  # a governance refusal is never an item-level error
                except Exception:
                    if not self.tolerate_errors:
                        raise
                    errors[_metric_name(ev)] = errors.get(_metric_name(ev), 0) + 1
                    continue
                by_metric.setdefault(s.metric, []).append(s.score)
                if isinstance(ev, LLMJudge):
                    judge = {"model": ev.judge_model, "prompt_version": ev.prompt_version}
                elif s.detail.get("judge_model"):
                    judge = {
                        "model": str(s.detail["judge_model"]),
                        "prompt_version": str(s.detail.get("prompt_version", "v1")),
                    }
        scores = {m: mean(v) for m, v in by_metric.items() if v}
        counts = {m: len(v) for m, v in by_metric.items() if v}
        return SuiteResult(self.name, scores, len(items), judge, counts, errors, non_proportion)


def sample_by_request_hash(items: list[EvalItem], n: int) -> list[EvalItem]:
    """Deterministically select ``n`` items ordered by request_hash (R5/GWT-3)."""
    ordered = sorted(items, key=lambda it: it.request_hash)
    return ordered[: max(0, n)]


def run_suite(
    suite: Suite,
    items: list[EvalItem],
    *,
    model: str,
    run_id: str,
    model_version: str | None = None,
    alias: str | None = None,
    dataset_revision: str | None = None,
    persist: bool = True,
    tenant: str = "default",
) -> SuiteResult:
    """Execute a suite and (by default) persist per-metric scores to platform_db (R6/R7).

    Each metric is recorded with the number of items it was actually computed over, so the
    Wilson interval ``record_eval_result`` puts around a proportion is sized by the real ``n``
    even when a suite skipped unlabelled items for one metric and not another.
    """
    result = suite.run(items)
    if persist and result.scores:
        from examlops.data.evaluation import record_eval_result

        groups: dict[int, dict[str, float]] = {}
        for metric, value in result.scores.items():
            groups.setdefault(result.counts.get(metric, result.sample_size), {})[metric] = value
        for n, scores in groups.items():
            record_eval_result(
                suite.name,
                model,
                scores,
                run_id=run_id,
                model_version=model_version,
                alias=alias,
                sample_size=n,
                judge=result.judge,
                dataset_revision=dataset_revision,
                non_proportion_metrics=result.non_proportion,
                tenant=tenant,
            )
    return result


def judge_calibration(judge_scores: list[float], human_labels: list[float]) -> dict[str, float]:
    """Compute judge↔human agreement over a labelled calibration set (R11)."""
    if not judge_scores or len(judge_scores) != len(human_labels):
        return {"agreement": 0.0, "n": 0}
    # Binary agreement at 0.5 threshold + mean absolute error.
    agree = sum(1 for j, h in zip(judge_scores, human_labels) if (j >= 0.5) == (h >= 0.5)) / len(
        judge_scores
    )
    mae = mean(abs(j - h) for j, h in zip(judge_scores, human_labels))
    return {"agreement": agree, "mae": mae, "n": len(judge_scores)}
