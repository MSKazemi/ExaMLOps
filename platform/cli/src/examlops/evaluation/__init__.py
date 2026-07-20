"""C2 — Continuous evaluation harness + LLM-as-judge (ADR 0007).

Engine-agnostic eval *suites* of *evaluators* (deterministic + LLM-as-judge) runnable
against A1-versioned datasets or a traffic sample, persisting scored results to
``platform_db.eval_suite_results``. The judge is any callable (wired to the B2 gateway in
production; a mock in tests) run at temperature 0 with its model + prompt version recorded.

The gate that *consumes* these scores is C3 (:mod:`examlops.evaluation.gate`).
"""

from __future__ import annotations

import hashlib
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

    @property
    def request_hash(self) -> str:
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


@dataclass
class LLMJudge:
    """Score outputs against a rubric via a judge callable, at temperature 0.

    ``judge_fn(prompt) -> float in [0,1]`` is the seam wired to the B2 gateway in
    production and mocked in tests. The judge model + prompt version are recorded per
    result for governance (R10).
    """

    judge_fn: Callable[[str], float]
    rubric: str
    judge_model: str = "judge"
    prompt_version: str = "v1"
    metric: str = "judge_score"

    def score(self, item: EvalItem) -> Score:
        prompt = (
            f"{self.rubric}\n\n[OUTPUT]\n{item.output}\n"
            f"[REFERENCE]\n{item.reference or ''}\nScore 0..1:"
        )
        raw = float(self.judge_fn(prompt))
        raw = max(0.0, min(1.0, raw))  # clamp to [0,1]
        return Score(
            self.metric,
            raw,
            {"judge_model": self.judge_model, "prompt_version": self.prompt_version},
        )


# ── Suite ─────────────────────────────────────────────────────────────────────


@dataclass
class SuiteResult:
    suite: str
    scores: dict[str, float]  # metric → mean score across items
    sample_size: int
    judge: dict[str, str] | None = None


@dataclass
class Suite:
    name: str
    evaluators: list[Any]

    def run(self, items: list[EvalItem]) -> SuiteResult:
        by_metric: dict[str, list[float]] = {}
        judge: dict[str, str] | None = None
        for item in items:
            for ev in self.evaluators:
                s = ev.score(item)
                by_metric.setdefault(s.metric, []).append(s.score)
                if isinstance(ev, LLMJudge):
                    judge = {"model": ev.judge_model, "prompt_version": ev.prompt_version}
        scores = {m: mean(v) for m, v in by_metric.items() if v}
        return SuiteResult(self.name, scores, len(items), judge)


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
) -> SuiteResult:
    """Execute a suite and (by default) persist per-metric scores to platform_db (R6/R7)."""
    result = suite.run(items)
    if persist:
        from examlops.data.evaluation import record_eval_result

        record_eval_result(
            suite.name,
            model,
            result.scores,
            run_id=run_id,
            model_version=model_version,
            alias=alias,
            sample_size=result.sample_size,
            judge=result.judge,
            dataset_revision=dataset_revision,
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
