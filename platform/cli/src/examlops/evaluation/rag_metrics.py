"""Retrieval-quality evaluators for the C2 harness, Ragas-backed (ADR 0019 decision 3).

``examlops.rag.context_precision``/``context_recall`` are plain functions a caller has to remember
to call. These evaluators put the same two measurements into the C2 :class:`~examlops.evaluation.
Suite`, so a RAG run is persisted to ``eval_suite_results`` like every other suite and the C3
regression gate (``exa eval gate`` / ``exa pipeline promote``) can gate a knowledge base on
``context_precision`` exactly as it gates a model on accuracy.

Backends
    ``ragas``  — Ragas' ``IDBasedContextPrecision`` / ``IDBasedContextRecall`` (the ``rag-eval``
               extra). These are Ragas' *non-LLM* retrieval metrics: they compare retrieved and
               reference context ids, so no judge model and no paid API is ever called.
    ``native`` — the set-membership math in :mod:`examlops.rag` (via the swappable ``rag_quality``
               provider when one is configured, ADR 0083).
    ``auto``   — ``ragas`` when importable, else ``native`` (the default; ``EXAMLOPS_RAG_EVAL_BACKEND``
               overrides).

The two backends agree by construction on every input with at least one retrieved id and one
reference id (both are hits/|retrieved| and hits/|reference|), which is why ``auto`` is a safe
default: installing Ragas changes where a number is computed, not the number. They differ on the
empty case, where Ragas returns NaN; a NaN is recorded here as ``0.0`` with ``detail["undefined"]``
so a suite mean is never poisoned by it.

An item carries its ids in ``EvalItem.metadata``: ``retrieved_ids`` (ranked) and ``relevant_ids``.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from examlops.evaluation import EvalItem, Score, Suite

BACKENDS = ("auto", "ragas", "native")
_ENV = "EXAMLOPS_RAG_EVAL_BACKEND"


class RagEvalBackendUnavailable(RuntimeError):
    """``ragas`` was requested explicitly and is not installed (or does not import)."""


def ragas_available() -> bool:
    try:
        _ragas_classes()
    except Exception:  # noqa: BLE001 - any import failure means "not usable here"
        return False
    return True


def _ragas_classes() -> tuple[Any, Any, Any]:
    from ragas.dataset_schema import SingleTurnSample
    from ragas.metrics._context_precision import IDBasedContextPrecision
    from ragas.metrics._context_recall import IDBasedContextRecall

    return SingleTurnSample, IDBasedContextPrecision, IDBasedContextRecall


def resolve_backend(name: str | None = None) -> str:
    """The concrete backend (``ragas`` | ``native``) a request resolves to."""
    chosen = (name or os.getenv(_ENV) or "auto").strip().lower()
    if chosen not in BACKENDS:
        raise ValueError(f"RAG eval backend {chosen!r} not in {BACKENDS}")
    if chosen == "auto":
        return "ragas" if ragas_available() else "native"
    if chosen == "ragas" and not ragas_available():
        raise RagEvalBackendUnavailable(
            "RAG eval backend 'ragas' requested but ragas is not installed — "
            "pip install 'examlops[rag-eval]', or use --backend native|auto"
        )
    return chosen


def _ids(item: EvalItem, key: str) -> list[str]:
    raw = item.metadata.get(key) or []
    return [str(x) for x in raw]


def _ragas_score(kind: str, retrieved: list[str], relevant: list[str]) -> float:
    sample_cls, precision_cls, recall_cls = _ragas_classes()
    metric = precision_cls() if kind == "precision" else recall_cls()
    sample = sample_cls(retrieved_context_ids=retrieved, reference_context_ids=relevant)
    return float(metric.single_turn_score(sample))


def _native_score(kind: str, retrieved: list[str], relevant: list[str]) -> float:
    from examlops.rag import context_precision, context_recall

    fn = context_precision if kind == "precision" else context_recall
    return float(fn(retrieved, relevant))


@dataclass
class _IdContextMetric:
    kind: str
    backend: str = "auto"
    metric: str = ""

    def __post_init__(self) -> None:
        if not self.metric:
            self.metric = f"context_{self.kind}"
        self.backend = resolve_backend(self.backend)

    def score(self, item: EvalItem) -> Score:
        retrieved, relevant = _ids(item, "retrieved_ids"), _ids(item, "relevant_ids")
        compute = _ragas_score if self.backend == "ragas" else _native_score
        value = compute(self.kind, retrieved, relevant)
        detail: dict[str, Any] = {"backend": self.backend}
        if math.isnan(value):
            value, detail["undefined"] = 0.0, True
        return Score(self.metric, max(0.0, min(1.0, value)), detail)


def ContextPrecision(backend: str = "auto") -> _IdContextMetric:  # noqa: N802 - evaluator ctor
    """Fraction of retrieved contexts that are relevant."""
    return _IdContextMetric("precision", backend)


def ContextRecall(backend: str = "auto") -> _IdContextMetric:  # noqa: N802 - evaluator ctor
    """Fraction of relevant contexts that were retrieved."""
    return _IdContextMetric("recall", backend)


def rag_retrieval_suite(name: str = "rag-retrieval", *, backend: str = "auto") -> Suite:
    """A C2 suite scoring ``context_precision`` + ``context_recall`` under one resolved backend."""
    resolved = resolve_backend(backend)
    return Suite(name, [ContextPrecision(resolved), ContextRecall(resolved)])


__all__ = [
    "BACKENDS",
    "ContextPrecision",
    "ContextRecall",
    "RagEvalBackendUnavailable",
    "rag_retrieval_suite",
    "ragas_available",
    "resolve_backend",
]
