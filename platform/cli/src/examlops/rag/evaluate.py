"""Evaluate a knowledge base's retrieval through the C2 harness (ADR 0019 decision 3).

A labelled question set — ``{"question": ..., "relevant_ids": [...]}`` per item — is run through
:meth:`examlops.rag.RagPipeline.retrieve` (retrieval only: no generation is paid for), each result
becomes a C2 :class:`~examlops.evaluation.EvalItem`, and :func:`examlops.evaluation.run_suite`
scores and persists ``context_precision`` / ``context_recall`` to ``eval_suite_results`` under the
model label ``rag:<kb>``. From there ``exa eval gate`` and ``exa eval history`` treat a knowledge base
like any other evaluated artifact.

Relevance may be labelled per document (``"runbook-7"``) or per chunk (``"runbook-7#2"``). If any
label names a chunk, retrieval is compared chunk-for-chunk; otherwise retrieved chunk ids are folded
to their document ids (first occurrence kept, rank preserved) so a document retrieved as three
chunks counts once, not three times.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from examlops.evaluation import EvalItem, run_suite
from examlops.evaluation.rag_metrics import rag_retrieval_suite

#: Upper bound on one evaluation run — a question set is a curated benchmark, not a traffic dump,
#: and each item costs a retrieval.
MAX_EVAL_ITEMS = 2000
#: Same ceiling as the serving endpoint's ``k`` (:data:`examlops.rag.service.MAX_K`).
MAX_EVAL_K = 50


class RagEvalInputError(ValueError):
    """The question set is malformed or too large."""


def _fold(retrieved: list[str], chunk_level: bool) -> list[str]:
    if chunk_level:
        return retrieved
    seen: list[str] = []
    for rid in retrieved:
        doc = rid.split("#", 1)[0]
        if doc not in seen:
            seen.append(doc)
    return seen


def _kb_state(kb: str, tenant: str) -> tuple[str | None, str | None]:
    """``(source_revision, updated_at)`` of the KB, or ``(None, None)`` if unknown."""
    try:
        from examlops.data import get_db, init_db

        init_db()
        with get_db() as conn:
            row = conn.execute(
                "SELECT source_revision, updated_at FROM rag_kbs WHERE kb=? AND tenant=?",
                (kb, tenant),
            ).fetchone()
    except Exception:  # noqa: BLE001 - provenance is best-effort; the scores are the result
        return None, None
    if not row:
        return None, None
    return (row["source_revision"] or None), (str(row["updated_at"]) if row["updated_at"] else None)


def validate_qa(qa: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not qa:
        raise RagEvalInputError("question set is empty")
    if len(qa) > MAX_EVAL_ITEMS:
        raise RagEvalInputError(f"question set has {len(qa)} items; the cap is {MAX_EVAL_ITEMS}")
    for i, row in enumerate(qa, 1):
        if not isinstance(row, dict) or not str(row.get("question", "")).strip():
            raise RagEvalInputError(f"item {i}: missing 'question'")
        rel = row.get("relevant_ids")
        if not isinstance(rel, list) or not rel:
            raise RagEvalInputError(f"item {i}: 'relevant_ids' must be a non-empty list")
    return qa


def build_eval_items(
    pipeline: Any, kb: str, qa: list[dict[str, Any]], *, tenant: str = "default", k: int = 5
) -> list[EvalItem]:
    items: list[EvalItem] = []
    for row in validate_qa(qa):
        question = str(row["question"])
        relevant = [str(x) for x in row["relevant_ids"]]
        chunk_level = any("#" in r for r in relevant)
        hits = pipeline.retrieve(kb, question, tenant=tenant, k=k)
        retrieved = _fold([h.id for h in hits], chunk_level)
        items.append(
            EvalItem(
                output=json.dumps(retrieved),
                prompt=question,
                metadata={"retrieved_ids": retrieved, "relevant_ids": relevant},
            )
        )
    return items


def evaluate_kb(
    kb: str,
    qa: list[dict[str, Any]],
    *,
    pipeline: Any = None,
    tenant: str = "default",
    k: int = 5,
    backend: str = "auto",
    suite: str = "rag-retrieval",
    model: str | None = None,
    run_id: str | None = None,
    dataset_revision: str | None = None,
    version: str | None = None,
    alias: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run the retrieval suite over ``qa`` against ``kb``; returns scores + provenance.

    ``version`` is the candidate the C3 gate compares (``exa eval gate run rag:<kb> <version>``);
    it defaults to the KB's recorded A1 ``source_revision``, so each re-ingest of a new source
    revision is a new gateable candidate. ``alias`` records the run as a baseline (e.g.
    ``Production``) for the gate to compare later candidates against.
    """
    from examlops.evaluation.rag_metrics import resolve_backend
    from examlops.rag import RagPipeline

    if not 1 <= int(k) <= MAX_EVAL_K:
        # Each item over-retrieves 3*k before reranking; an unbounded k is an unbounded scan.
        raise RagEvalInputError(f"k must be between 1 and {MAX_EVAL_K}, got {k}")
    resolved = resolve_backend(backend)
    pipe = pipeline if pipeline is not None else RagPipeline()
    items = build_eval_items(pipe, kb, qa, tenant=tenant, k=k)
    # One label per (tenant, kb): two tenants' KBs of the same name are different artifacts and
    # must not share an eval series or a gate.
    label = model or (f"rag:{kb}" if tenant == "default" else f"rag:{tenant}/{kb}")
    revision, updated_at = _kb_state(kb, tenant)
    if version is None:
        version = revision
    # Deterministic per (kb state, tenant, question set, k, retrieval): re-running the same
    # benchmark on an unchanged KB is idempotent in `eval_suite_results` rather than a duplicate
    # trend point, while a re-ingest (new `updated_at`) is a new measurement. Everything that
    # makes a run a *different record* is in the basis: `eval_suite_results` inserts with
    # INSERT OR IGNORE on (suite, model_version, run_id, metric), so a rerun that only adds
    # `--alias Production` (recording the gate's baseline) or switches backend/label/dataset
    # revision would otherwise be silently dropped as a duplicate of the earlier run.
    basis = json.dumps(
        [
            kb,
            tenant,
            updated_at,
            version,
            k,
            getattr(pipe, "retrieval", "dense"),
            label,
            alias,
            resolved,
            dataset_revision,
            qa,
        ],
        sort_keys=True,
        default=str,
    )
    rid = run_id or f"{suite}:{hashlib.sha256(basis.encode()).hexdigest()[:16]}"
    result = run_suite(
        rag_retrieval_suite(suite, backend=resolved),
        items,
        model=label,
        run_id=rid,
        model_version=version,
        alias=alias,
        dataset_revision=dataset_revision,
        persist=persist,
    )
    return {
        "suite": suite,
        "model": label,
        "kb": kb,
        "tenant": tenant,
        "backend": resolved,
        "k": k,
        "run_id": rid,
        "version": version,
        "alias": alias,
        "sample_size": result.sample_size,
        "scores": result.scores,
    }


__all__ = [
    "MAX_EVAL_ITEMS",
    "MAX_EVAL_K",
    "RagEvalInputError",
    "build_eval_items",
    "evaluate_kb",
    "validate_qa",
]
