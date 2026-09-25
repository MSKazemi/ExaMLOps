"""Evaluator engines — DeepEval and Ragas wrapped behind the harness seam (ADR 0007 decision 1).

The ADR decides to *wrap* DeepEval and Ragas for their metrics rather than reinvent them, while
keeping an internal abstraction so the engine is swappable. This module is that abstraction:
every metric, whichever library computes it, is an object with ``metric`` and ``score(item)``
(the :class:`~examlops.evaluation.Evaluator` protocol), built from a short **spec string**:

==========================  ===================================================================
spec                        evaluator
==========================  ===================================================================
``exact_match``             :class:`~examlops.evaluation.ExactMatch` (needs a reference)
``json_valid``              :class:`~examlops.evaluation.JSONValid`
``numeric_match[:TOL]``     :class:`~examlops.evaluation.NumericTolerance` (needs a reference)
``abs_error``               per-item ``|output − reference|``; its suite mean is the MAE the
                            ground-truth loop reports (``exa eval feedback accuracy``)
``string_presence``         reference ⊂ output — Ragas ``StringPresence`` or the identical
                            pure-python formula
``string_similarity``       1 − normalised Levenshtein — Ragas ``NonLLMStringSimilarity``, the
                            rapidfuzz call it wraps, or the identical pure-python edit distance
``rouge_l``                 rougeL F1, Porter-stemmed — Ragas ``RougeScore`` or the rouge-score
                            call it wraps; with neither, ``rouge_l_unstemmed`` — recorded under
                            a **different metric name**, because without the stemmer it is a
                            different number
``judge:RUBRIC``            :class:`~examlops.evaluation.LLMJudge` with a built-in rubric
                            (``correctness`` / ``relevancy`` / ``faithfulness`` / ``helpfulness``)
                            answered by the B2 gateway at temperature 0
``deepeval:METRIC``         DeepEval ``AnswerRelevancy`` / ``Faithfulness`` / ``ContextualPrecision``
                            / ``ContextualRecall`` / ``Hallucination``, with the gateway as its
                            model at temperature 0; recorded as ``deepeval_<metric>``
==========================  ===================================================================

**Optional, lazily imported, never silently substituted.** Neither library can be a workspace
extra today (deepeval 4.2.6 pins ``click<8.4`` against this package's ``click>=8.5``; ragas 0.4.3
pulls ``datasets``, which caps ``fsspec<=2026.6`` against the dataplane's ``fsspec>=2026.7``), so
both activate when importable — e.g. in a separate evaluator image — and the ``eval-metrics``
extra installs the two libraries Ragas's text metrics delegate to (rapidfuzz, rouge-score).
A ``deepeval:`` spec with DeepEval absent raises
:class:`EvaluatorUnavailable` — a rubric judge is not the same measurement and is not swapped
in behind the operator's back (``judge:relevancy`` is the explicit alternative). Where a
pure-python formula *is* the same measurement it is used as the fallback under the same metric
name, and ``engine`` in each score's detail says which one ran. Both libraries' telemetry is
switched off before import (``DEEPEVAL_TELEMETRY_OPT_OUT`` / ``RAGAS_DO_NOT_TRACK``): the harness
runs in-cluster and eval content must not leave it (the ADR's rejected-SaaS alternative).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from examlops.evaluation import (
    JUDGE_TEMPERATURE,
    EvalItem,
    ExactMatch,
    JSONValid,
    LLMJudge,
    NumericTolerance,
    Score,
)

ENGINE_NATIVE = "examlops"
ENGINE_RAGAS = "ragas"
ENGINE_DEEPEVAL = "deepeval"

#: Built-in rubrics for ``judge:<name>``. Versioned: the version is recorded per result (d4).
RUBRICS: dict[str, tuple[str, str]] = {
    "correctness": (
        "v1",
        "Grade whether OUTPUT is factually correct and agrees with REFERENCE when one is given. "
        "1 = fully correct, 0 = wrong.",
    ),
    "relevancy": (
        "v1",
        "Grade whether OUTPUT directly and completely answers the user's request. "
        "1 = fully relevant, 0 = off-topic.",
    ),
    "faithfulness": (
        "v1",
        "Grade whether every claim in OUTPUT is supported by REFERENCE (the source context). "
        "1 = fully supported, 0 = unsupported or contradicted.",
    ),
    "helpfulness": (
        "v1",
        "Grade how helpful, clear and actionable OUTPUT is for an operator. "
        "1 = very helpful, 0 = unhelpful.",
    ),
}

#: DeepEval metric class per spec name, and whether it needs a reference / retrieval context.
DEEPEVAL_METRICS: dict[str, tuple[str, bool, bool]] = {
    "answer_relevancy": ("AnswerRelevancyMetric", False, False),
    "faithfulness": ("FaithfulnessMetric", False, True),
    "contextual_precision": ("ContextualPrecisionMetric", True, True),
    "contextual_recall": ("ContextualRecallMetric", True, True),
    "hallucination": ("HallucinationMetric", False, True),
}

RAGAS_SPECS = ("string_presence", "string_similarity", "rouge_l")
NATIVE_SPECS = ("exact_match", "json_valid", "numeric_match", "abs_error")


class EvaluatorUnavailable(RuntimeError):
    """A spec names an engine that is not installed, or needs a judge model nobody configured."""


class UnknownEvaluator(ValueError):
    """A spec this registry does not know."""


def _quiet_telemetry() -> None:
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")


def module_available(name: str) -> bool:
    """Whether ``name`` is importable, without importing it (and so without its side effects)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def available_engines() -> dict[str, bool]:
    """Which optional engines this interpreter can use."""
    return {
        ENGINE_NATIVE: True,
        ENGINE_RAGAS: module_available("ragas"),
        ENGINE_DEEPEVAL: module_available("deepeval"),
    }


# ── native deterministic additions ────────────────────────────────────────────


@dataclass
class AbsoluteError:
    """``|output − reference|`` per item; the suite mean is the MAE. Carries a unit."""

    metric: str = "mae"
    requires_reference: bool = True
    proportion: bool = False

    def score(self, item: EvalItem) -> Score:
        got, ref = float(item.output), float(item.reference)  # type: ignore[arg-type]
        return Score(self.metric, abs(got - ref), {"engine": ENGINE_NATIVE})


def levenshtein(a: str, b: str) -> int:
    """Edit distance, O(len(a)·len(b)) time and O(min) memory."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _lcs(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def _tokens(text: str) -> list[str]:
    # rouge-score's default tokenizer: lowercase, non-alphanumerics to spaces.
    return "".join(c.lower() if c.isalnum() else " " for c in text).split()


def rouge_l_f1(reference: str, response: str) -> float:
    """ROUGE-L F1 over lowercase alphanumeric tokens, no stemming."""
    ref, hyp = _tokens(reference), _tokens(response)
    lcs = _lcs(ref, hyp)
    if lcs == 0:
        return 0.0
    precision, recall = lcs / len(hyp), lcs / len(ref)
    return 2 * precision * recall / (precision + recall)


#: Text longer than this is truncated before scoring — a pasted log must not stall a cycle.
MAX_TEXT_CHARS = 20_000
#: The pure-python edit distance is O(n·m) in the interpreter; it gets a tighter bound.
PURE_MAX_CHARS = 2_000

#: The library Ragas itself delegates each text metric to (``ragas.metrics.collections``).
_TEXT_LIBS = {"string_similarity": "rapidfuzz", "rouge_l": "rouge_score"}
_RAGAS_CLASSES = {
    "string_presence": "StringPresence",
    "string_similarity": "NonLLMStringSimilarity",
    "rouge_l": "RougeScore",
}


def text_engine(name: str) -> str:
    """Which engine :class:`RagasTextMetric` would use for ``name`` in this interpreter.

    ``ragas`` when Ragas (and the library its metric needs) is importable; ``lib`` when only that
    underlying library is (the ``eval-metrics`` extra — the same computation Ragas performs);
    ``examlops`` for the pure-python formula.
    """
    lib = _TEXT_LIBS.get(name)
    if module_available("ragas") and (lib is None or module_available(lib)):
        return ENGINE_RAGAS
    if lib is not None and module_available(lib):
        return "lib"
    return ENGINE_NATIVE


@dataclass
class RagasTextMetric:
    """A reference-vs-response text metric, computed as Ragas computes it.

    Three engines, one number: Ragas's own class when Ragas is installed; otherwise the library
    Ragas delegates to (rapidfuzz's normalised Levenshtein, rouge-score's Porter-stemmed rougeL
    F1); otherwise a pure-python formula. ``string_presence`` and ``string_similarity`` are
    identical in all three. The pure ROUGE-L cannot stem, so it is recorded as
    ``rouge_l_unstemmed`` — a different number must not share a series with the stemmed one.
    """

    name: str  # string_presence | string_similarity | rouge_l
    requires_reference: bool = True
    engine: str = field(init=False)
    metric: str = field(init=False)
    _impl: Any = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if self.name not in RAGAS_SPECS:
            raise UnknownEvaluator(f"unknown text metric {self.name!r}")
        self.engine = text_engine(self.name)
        self.metric = self.name
        if self.engine == ENGINE_RAGAS:
            _quiet_telemetry()
            collections = importlib.import_module("ragas.metrics.collections")
            self._impl = getattr(collections, _RAGAS_CLASSES[self.name])()
        elif self.engine == "lib" and self.name == "rouge_l":
            scorer = importlib.import_module("rouge_score.rouge_scorer")
            self._impl = scorer.RougeScorer(["rougeL"], use_stemmer=True)
        elif self.engine == "lib":
            self._impl = importlib.import_module("rapidfuzz.distance").Levenshtein
        elif self.name == "rouge_l":
            self.metric = "rouge_l_unstemmed"

    def _value(self, reference: str, response: str) -> float:
        if self.engine == ENGINE_RAGAS:
            result = self._impl.score(reference=reference, response=response)
            return float(getattr(result, "value", result))
        if self.name == "string_presence":
            return 1.0 if reference in response else 0.0
        if self.name == "string_similarity":
            if self.engine == "lib":
                return 1.0 - float(self._impl.normalized_distance(reference, response))
            longest = max(len(reference), len(response))
            return 1.0 if longest == 0 else 1.0 - levenshtein(reference, response) / longest
        if self.engine == "lib":
            return float(self._impl.score(reference, response)["rougeL"].fmeasure)
        return rouge_l_f1(reference, response)

    def score(self, item: EvalItem) -> Score:
        cap = (
            PURE_MAX_CHARS
            if (self.engine == ENGINE_NATIVE and self.name != "rouge_l")
            else (MAX_TEXT_CHARS)
        )
        reference, response = str(item.reference or ""), str(item.output)
        truncated = len(reference) > cap or len(response) > cap
        value = self._value(reference[:cap], response[:cap])
        detail: dict[str, Any] = {"engine": self.engine}
        if truncated:
            detail["truncated_to"] = cap
        return Score(self.metric, value, detail)


# ── DeepEval ──────────────────────────────────────────────────────────────────


def _deepeval_model(generate: Callable[..., str], name: str) -> Any:
    """A DeepEval ``DeepEvalBaseLLM`` whose every call is ``generate(prompt, temperature=0)``."""
    base = importlib.import_module("deepeval.models").DeepEvalBaseLLM

    class _GatewayModel(base):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            self._generate = generate
            self._name = name
            super().__init__(model=name)

        def load_model(self, *args: Any, **kwargs: Any) -> Any:
            return self

        def generate(self, prompt: Any, *args: Any, **kwargs: Any) -> str:
            # ``schema`` is accepted and ignored: DeepEval parses the JSON out of the text.
            return self._generate(str(prompt), temperature=JUDGE_TEMPERATURE)

        async def a_generate(self, prompt: Any, *args: Any, **kwargs: Any) -> str:
            return self.generate(prompt)

        def get_model_name(self, *args: Any, **kwargs: Any) -> str:
            return self._name

        def supports_temperature(self) -> bool:
            return True

    return _GatewayModel()


@dataclass
class DeepEvalMetric:
    """One DeepEval metric, scored by the gateway judge model at temperature 0."""

    name: str
    generate: Callable[..., str]
    judge_model: str
    threshold: float = 0.5
    metric: str = field(init=False)
    requires_reference: bool = field(init=False)
    requires_context: bool = field(init=False)
    prompt_version: str = field(init=False)
    _metric: Any = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if self.name not in DEEPEVAL_METRICS:
            raise UnknownEvaluator(
                f"unknown deepeval metric {self.name!r}; known: {', '.join(DEEPEVAL_METRICS)}"
            )
        cls_name, self.requires_reference, self.requires_context = DEEPEVAL_METRICS[self.name]
        self.metric = f"deepeval_{self.name}"
        if not module_available("deepeval"):
            raise EvaluatorUnavailable(
                f"deepeval:{self.name} needs DeepEval — install the extra: "
                "install deepeval in the evaluator's environment (or use judge:<rubric>)"
            )
        _quiet_telemetry()
        metrics = importlib.import_module("deepeval.metrics")
        self.prompt_version = f"deepeval-{_dist_version('deepeval')}"
        cls = getattr(metrics, cls_name)
        model = _deepeval_model(self.generate, self.judge_model)
        kwargs: dict[str, Any] = {
            "model": model,
            "threshold": self.threshold,
            "async_mode": False,
            "include_reason": False,
        }
        try:
            # DeepEval >= 4 can route a metric to a non-LLM classifier ("system_one"); the
            # harness asks for the LLM path so the judge it records is the judge that ran.
            self._metric = cls(eval_mode="llm", **kwargs)
        except TypeError:
            self._metric = cls(**kwargs)

    def score(self, item: EvalItem) -> Score:
        if self.requires_context and not item.contexts:
            raise ValueError(f"{self.metric} needs retrieval contexts on the item")
        test_case_cls = importlib.import_module("deepeval.test_case").LLMTestCase
        case = test_case_cls(
            input=item.prompt or "",
            actual_output=item.output,
            expected_output=item.reference,
            retrieval_context=list(item.contexts or []) or None,
            context=list(item.contexts or []) or None,
        )
        value = self._metric.measure(case, _show_indicator=False)
        value = getattr(self._metric, "score", value)
        return Score(
            self.metric,
            max(0.0, min(1.0, float(value))),
            {
                "engine": ENGINE_DEEPEVAL,
                "judge_model": self.judge_model,
                "prompt_version": self.prompt_version,
                "temperature": JUDGE_TEMPERATURE,
                "temperature_enforced": True,
            },
        )


def _dist_version(dist: str) -> str:
    try:
        from importlib.metadata import version

        return version(dist)
    except Exception:  # noqa: BLE001 - a missing dist-info must not break scoring
        return "unknown"


# ── registry ──────────────────────────────────────────────────────────────────


def known_specs() -> list[str]:
    """Every spec shape :func:`make_evaluator` accepts."""
    return [
        *NATIVE_SPECS,
        "numeric_match:TOL",
        *RAGAS_SPECS,
        *(f"judge:{r}" for r in RUBRICS),
        *(f"deepeval:{m}" for m in DEEPEVAL_METRICS),
    ]


def needs_judge(spec: str) -> bool:
    return spec.startswith(("judge:", "deepeval:"))


def make_evaluator(
    spec: str,
    *,
    judge_model: str | None = None,
    judge_client: Any = None,
    text_fn: Callable[..., str] | None = None,
) -> Any:
    """Build the evaluator a spec names. Raises :class:`UnknownEvaluator` / ``EvaluatorUnavailable``.

    ``judge:`` and ``deepeval:`` specs need ``judge_model`` (a gateway model name). ``text_fn``
    overrides the gateway generator — the seam tests use; production passes nothing.
    """
    spec = spec.strip()
    kind, _, arg = spec.partition(":")
    if kind == "exact_match" and not arg:
        ev: Any = ExactMatch()
        ev.requires_reference = True
        return ev
    if kind == "json_valid" and not arg:
        return JSONValid()
    if kind == "numeric_match":
        try:
            tol = float(arg) if arg else 1e-6
        except ValueError as exc:
            raise UnknownEvaluator(f"numeric_match tolerance must be a number: {arg!r}") from exc
        if tol < 0:
            raise UnknownEvaluator("numeric_match tolerance must be >= 0")
        ev = NumericTolerance(tolerance=tol)
        ev.requires_reference = True
        return ev
    if kind == "abs_error" and not arg:
        return AbsoluteError()
    if kind in RAGAS_SPECS and not arg:
        return RagasTextMetric(kind)
    if kind in ("judge", "deepeval"):
        if not judge_model:
            raise EvaluatorUnavailable(f"{spec} needs a judge model (--judge-model)")
        from examlops.evaluation.judges import gateway_judge, gateway_text_fn

        if kind == "judge":
            if arg not in RUBRICS:
                raise UnknownEvaluator(f"unknown rubric {arg!r}; known: {', '.join(RUBRICS)}")
            version, rubric = RUBRICS[arg]
            if text_fn is not None:
                from examlops.evaluation.judges import parse_score

                gen = text_fn

                def judge_fn(prompt: str, *, temperature: float = JUDGE_TEMPERATURE) -> float:
                    return parse_score(gen(prompt, temperature=temperature))

            else:
                judge_fn = gateway_judge(judge_model, client=judge_client)
            return LLMJudge(
                judge_fn=judge_fn,
                rubric=rubric,
                judge_model=judge_model,
                prompt_version=f"{arg}-{version}",
                metric=f"judge_{arg}",
            )
        generate = text_fn or gateway_text_fn(judge_model, client=judge_client, max_tokens=1024)
        return DeepEvalMetric(arg, generate, judge_model)
    raise UnknownEvaluator(f"unknown evaluator {spec!r}; known: {', '.join(known_specs())}")


def describe() -> list[dict[str, Any]]:
    """Each spec with the engine that would compute it here — for ``exa eval evaluators``."""
    engines = available_engines()
    rows: list[dict[str, Any]] = []
    for spec in NATIVE_SPECS:
        rows.append({"spec": spec, "engine": ENGINE_NATIVE, "available": True, "judge": False})
    for spec in RAGAS_SPECS:
        engine = text_engine(spec)
        engine_label = _TEXT_LIBS.get(spec, ENGINE_NATIVE) if engine == "lib" else engine
        metric = "rouge_l_unstemmed" if (spec == "rouge_l" and engine == ENGINE_NATIVE) else spec
        rows.append(
            {
                "spec": spec,
                "engine": engine_label,
                "available": True,
                "judge": False,
                "metric": metric,
            }
        )
    for rubric in RUBRICS:
        rows.append(
            {"spec": f"judge:{rubric}", "engine": "gateway", "available": True, "judge": True}
        )
    for name in DEEPEVAL_METRICS:
        rows.append(
            {
                "spec": f"deepeval:{name}",
                "engine": ENGINE_DEEPEVAL,
                "available": engines[ENGINE_DEEPEVAL],
                "judge": True,
            }
        )
    return rows


def parse_specs(raw: str | list[str] | None) -> list[str]:
    """Specs from a JSON list, a comma-separated string, or a list — deduplicated, in order."""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        items = json.loads(text) if text.startswith("[") else text.split(",")
    else:
        items = raw
    out: list[str] = []
    for spec in items:
        s = str(spec).strip()
        if s and s not in out:
            out.append(s)
    return out


__all__ = [
    "AbsoluteError",
    "DeepEvalMetric",
    "EvaluatorUnavailable",
    "RagasTextMetric",
    "UnknownEvaluator",
    "available_engines",
    "describe",
    "known_specs",
    "levenshtein",
    "make_evaluator",
    "needs_judge",
    "parse_specs",
    "rouge_l_f1",
]
