"""B4 — RAG pipeline & retrieval ops (ADR 0019).

Ingest (load→chunk→embed→index) and Query (embed→retrieve→rerank→assemble→generate)
over the B5 vector store, with gateway-routed generation (B2) using a versioned B1 prompt,
a RETRIEVER span (C1), pluggable reranking, per-tenant isolation (D6), KB versioning against
A1 source revisions, and a D8 guardrail seam over untrusted retrieved content.

Every dependency degrades: with no embedding service the deterministic token-hash embedding
is used; with no gateway a local echo generator answers; the vector store is the B5 SQLite
fallback — so the full RAG round-trip runs with no external service.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_EMBED_DIM = 64
# Minimal D8 seam — prompt-injection patterns in retrieved (untrusted) content.
_INJECTION = re.compile(
    r"(ignore (all |the )?previous instructions|disregard .* above|system prompt|you are now)",
    re.IGNORECASE,
)


@dataclass
class Citation:
    doc_id: str
    score: float


@dataclass
class RagAnswer:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    retrieval_span_id: str | None = None
    guardrail_flagged: bool = False
    contexts: list[str] = field(default_factory=list)
    #: B8 structured answer (``query(..., structured=True)``): the validated object plus the
    #: citation-grounding verdict. ``None`` for a free-text answer. With a structured answer,
    #: ``citations`` are the chunks the model validly cited (ADR 0035 clause 1/3), not every hit.
    structured: dict[str, Any] | None = None
    grounded: bool | None = None
    #: Citations the model made that name no retrieved chunk — dropped, never returned. Kept as the
    #: model sent them, for diagnosis.
    dropped_citations: list[Any] = field(default_factory=list)


# ── embedding + chunking (pure) ───────────────────────────────────────────────


def default_embed(text: str) -> list[float]:
    """Deterministic token-hash embedding — ingest and query share this space."""
    vec = [0.0] * _EMBED_DIM
    for tok in text.lower().split():
        h = int(
            hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest(), 16
        )  # bucketing, not security
        vec[h % _EMBED_DIM] += 1.0
    return vec


def chunk_text(text: str, size: int = 40, overlap: int = 10) -> list[str]:
    """Split text into overlapping word-window chunks (R1) — the ``native`` framework chunker."""
    from examlops.rag.frameworks import native_split

    return native_split(text, size, overlap)


# ── rerankers (pluggable, R2/GWT-3) ───────────────────────────────────────────


def identity_reranker(question: str, hits: list) -> list:
    return hits


def lexical_reranker(question: str, hits: list) -> list:
    """Re-order by lexical overlap between the question and each hit's text metadata."""
    q_tokens = set(question.lower().split())

    def overlap(hit) -> float:
        text = str(hit.metadata.get("text", "")).lower()
        toks = set(text.split())
        return len(q_tokens & toks) / (len(q_tokens) or 1)

    return sorted(hits, key=overlap, reverse=True)


# ── guardrails (D8 seam, R7) ──────────────────────────────────────────────────

_guardrail: Callable[[str], tuple[bool, str]] | None = None


def set_guardrail(fn: Callable[[str], tuple[bool, str]]) -> None:
    """Install the D8 guardrail: text -> (flagged, neutralized_text)."""
    global _guardrail
    _guardrail = fn


def _apply_guardrail(text: str) -> tuple[bool, str]:
    if _guardrail is not None:
        return _guardrail(text)
    # Default minimal guardrail: flag + de-fang injection, keep the content as data.
    if _INJECTION.search(text):
        return True, _INJECTION.sub("[redacted-instruction]", text)
    return False, text


# ── pipeline ──────────────────────────────────────────────────────────────────


RETRIEVAL_MODES = ("dense", "hybrid")


@dataclass
class RagPipeline:
    embed_fn: Callable[[str], list[float]] = default_embed
    reranker: Callable[[str, list], list] = lexical_reranker
    chunk_size: int = 40
    chunk_overlap: int = 10
    # "dense" (embedding only) or "hybrid" (embedding + BM25, rank-fused; ADR 0020 clause 2).
    # Hybrid recovers chunks that name an exact identifier — a job id, an error code, a model
    # name — which an embedding blurs. Dense stays the default so existing answers do not move.
    retrieval: str = "dense"
    fusion: str = "rrf"
    #: Identity of the encoder behind ``embed_fn`` (ADR 0043). The built-in embedding is
    #: ``token-hash``; pass your own id with your own ``embed_fn``. A query is checked against the
    #: collection's stamp under *this* id, so a pipeline whose encoder differs from the one that
    #: built the knowledge base is refused instead of scoring vectors that mean nothing.
    encoder_id: str | None = None
    #: Which framework chunks documents at ingest (ADR 0019 decision 1): ``native`` | ``llamaindex``
    #: | ``auto``; ``None`` reads ``EXAMLOPS_RAG_FRAMEWORK`` (default ``native``). See
    #: :mod:`examlops.rag.frameworks`.
    framework: str | None = None
    #: Per-pipeline D8 guardrail over retrieved content, ``text -> (flagged, neutralized)``. ``None``
    #: uses the process-wide one from :func:`set_guardrail` (or the built-in injection filter). The
    #: RAG service sets one per tenant so two tenants never share a guardrail's state or mode.
    guardrail: Callable[[str], tuple[bool, str]] | None = None

    def __post_init__(self) -> None:
        if self.retrieval not in RETRIEVAL_MODES:
            raise ValueError(f"retrieval '{self.retrieval}' not in {RETRIEVAL_MODES}")
        if self.encoder_id is None and self.embed_fn is default_embed:
            self.encoder_id = "token-hash"

    def _kb_encoder(self, kb: str, tenant: str) -> str | None:
        """The encoder this KB was ingested with, or ``None`` if it was never recorded.

        ``None`` is deliberately not an error: a KB ingested before the encoder was stamped has
        nothing to compare against, and refusing would break every corpus already indexed.
        """
        try:
            from examlops.data import get_db

            with get_db() as conn:
                row = conn.execute(
                    "SELECT encoder FROM rag_kbs WHERE kb=? AND tenant=?", (kb, tenant)
                ).fetchone()
            return str(row["encoder"]) if row and row["encoder"] else None
        except Exception:
            return None

    def _store(self):
        from examlops.vector_store import select_store

        return select_store()

    def ingest(
        self,
        kb: str,
        docs: list[dict[str, str]],
        *,
        tenant: str = "default",
        source_revision: str | None = None,
        encoder: str | None = None,
    ) -> int:
        """Chunk→embed→index documents into the B5 store; version against A1 (R1).

        ``encoder`` overrides the id stamped on the collection; by default it is the pipeline's
        own ``encoder_id``.
        """
        from examlops.data import get_db, init_db
        from examlops.rag.frameworks import get_chunker
        from examlops.vector_store import VecItem

        encoder = encoder or self.encoder_id or "token-hash"
        chunker = get_chunker(self.framework)  # before any write: an unavailable one fails clean

        init_db()
        store = self._store()
        try:
            # The encoder is stamped on the collection, not merely recorded in `rag_kbs`
            # alongside it (ADR 0043 clause 2). It was already carried into this function and
            # written to that table, and never reached the store — so the B5 guard added for
            # exactly this had nothing to compare against.
            store.create_collection(kb, _EMBED_DIM, "cosine", tenant, encoder_id=encoder)
        except Exception:
            pass
        items: list[VecItem] = []
        for doc in docs:
            doc_id = doc["id"]
            for ci, chunk in enumerate(
                chunker.split(doc["text"], self.chunk_size, self.chunk_overlap)
            ):
                items.append(
                    VecItem(
                        f"{doc_id}#{ci}",
                        self.embed_fn(chunk),
                        {"text": chunk, "doc_id": doc_id, "source_revision": source_revision or ""},
                        text=chunk,
                    )
                )
        store.upsert(kb, items, tenant, encoder_id=encoder)
        with get_db() as conn:
            conn.execute(
                """INSERT INTO rag_kbs (kb, tenant, source_revision, encoder, chunk_count)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(kb, tenant) DO UPDATE SET
                       source_revision=excluded.source_revision, encoder=excluded.encoder,
                       chunk_count=excluded.chunk_count, updated_at=CURRENT_TIMESTAMP""",
                (kb, tenant, source_revision, encoder, len(items)),
            )
        from examlops.data.audit import audit_best_effort

        audit_best_effort(
            "exa-rag",
            None,
            "rag_ingest",
            f"{tenant}/{kb}",
            {
                "docs": len(docs),
                "chunks": len(items),
                "framework": chunker.name,
                "encoder": encoder,
                "source_revision": source_revision,
            },
            tenant=tenant,
        )
        return len(items)

    def retrieve(self, kb: str, question: str, *, tenant: str = "default", k: int = 5) -> list[Any]:
        """embed→retrieve→rerank, trimmed to ``k`` — the retrieval half of :meth:`query`.

        Exposed on its own so retrieval quality can be evaluated (``exa rag eval``) without paying
        for a generation per question.
        """
        store = self._store()
        qv = self.embed_fn(question)
        # Ask under the encoder that produced *this query's* vector, so the store can compare it
        # with the one that built the collection and refuse a mismatch rather than return ranked
        # nonsense — retrieval is where cross-encoder scoring is most convincing and least
        # detectable, because every hit still arrives with a plausible score and a real citation.
        # Passing the KB's own recorded encoder here, as this used to, compared the collection
        # with itself: the guard could never fire. A pipeline with a custom ``embed_fn`` and no
        # ``encoder_id`` still falls back to the KB's record, because it has nothing to claim.
        encoder_id = (
            self.encoder_id if self.encoder_id is not None else self._kb_encoder(kb, tenant)
        )
        if self.retrieval == "hybrid":
            hits = store.hybrid_search(
                kb,
                qv,
                question,
                max(k * 3, k),
                None,
                tenant,
                encoder_id=encoder_id,
                fusion=self.fusion,
            )
        else:
            hits = store.search(
                kb, qv, max(k * 3, k), None, tenant, encoder_id=encoder_id
            )  # over-retrieve for rerank
        return list(self.reranker(question, hits)[:k])  # rerank then trim (GWT-3)

    def query(
        self,
        kb: str,
        question: str,
        *,
        tenant: str = "default",
        k: int = 5,
        generate_fn: Callable[[str], str] | None = None,
        prompt_label: str | None = None,
        structured: bool = False,
    ) -> RagAnswer:
        """embed→retrieve→rerank→assemble→generate, citing chunks (R2/R3/GWT-1).

        ``structured=True`` produces the answer through B8 structured output
        (:mod:`examlops.rag.grounded`): a schema-valid object whose citations must name retrieved
        chunks, with ``RagAnswer.grounded`` saying whether any valid citation survived.
        """
        hits = self.retrieve(kb, question, tenant=tenant, k=k)

        span_id = self._retriever_span(question, hits, tenant)

        # D8 guardrail over untrusted retrieved content (R7/GWT-6).
        flagged = False
        contexts: list[str] = []
        guard = self.guardrail or _apply_guardrail
        for h in hits:
            f, safe = guard(str(h.metadata.get("text", "")))
            flagged = flagged or f
            contexts.append(safe)

        prompt = self._assemble_prompt(question, contexts, prompt_label)
        gen = generate_fn or self._default_generate
        if structured:
            from examlops.rag.grounded import generate_grounded

            ga = generate_grounded(
                prompt,
                len(contexts),
                generate_fn=generate_fn or self._schema_generate,
                model=f"rag:{kb}",
                tenant=tenant,
            )
            return RagAnswer(
                answer=ga.answer,
                # Only the chunks the answer validly cites (ADR 0035): returning every retrieved
                # hit would let an ungrounded answer carry citations it never made.
                citations=[Citation(hits[i - 1].id, hits[i - 1].score) for i in ga.citations],
                retrieval_span_id=span_id,
                guardrail_flagged=flagged,
                contexts=contexts,
                structured={
                    **ga.as_dict(),
                    "cited_chunks": [hits[i - 1].id for i in ga.citations],
                },
                grounded=ga.grounded,
                dropped_citations=list(ga.dropped_citations),
            )
        answer = gen(prompt)
        return RagAnswer(
            answer=answer,
            citations=[Citation(h.id, h.score) for h in hits],
            retrieval_span_id=span_id,
            guardrail_flagged=flagged,
            contexts=contexts,
        )

    def _assemble_prompt(self, question: str, contexts: list[str], label: str | None) -> str:
        ctx = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts))
        if label:
            try:
                from examlops.prompts import get_prompt, render

                pv = get_prompt("rag", label)
                return render(pv, context=ctx, question=question)
            except Exception:
                pass
        return (
            "Answer the question using only the context. Cite chunk numbers.\n\n"
            f"Context:\n{ctx}\n\nQuestion: {question}\nAnswer:"
        )

    def _schema_generate(self, prompt: str) -> Any:
        """Default structured generator: constrained decoding at the gateway (ADR 0035).

        Asks the gateway for the registered ``rag_answer`` schema and returns its parsed object;
        :func:`examlops.rag.grounded.parse_json_answer` passes a dict through unchanged. A D5
        refusal propagates; any other gateway failure degrades to the free-text default, whose
        output B8 then repairs and the grounding check marks ungrounded — visible, never silent.
        """
        try:
            from examlops.gateway import (
                GatewayClient,
                ReasoningPolicyDenied,
                build_default_router,
            )
        except Exception:  # noqa: BLE001 - no gateway at all: the offline placeholder
            return self._default_generate(prompt)
        try:
            comp = GatewayClient(build_default_router()).chat(
                "default", [{"role": "user", "content": prompt}], response_schema="rag_answer"
            )
        except ReasoningPolicyDenied:
            raise
        except Exception:  # noqa: BLE001 - degrade, see docstring
            return self._default_generate(prompt)
        return comp.parsed if comp.parsed is not None else comp.text

    def _default_generate(self, prompt: str) -> str:
        try:
            from examlops.gateway import (
                GatewayClient,
                ReasoningPolicyDenied,
                build_default_router,
            )
        except Exception:  # noqa: BLE001 - no gateway at all: the offline placeholder below
            return prompt.split("Answer:")[-1].strip() or "(no answer)"
        try:
            comp = GatewayClient(build_default_router()).chat(
                "default", [{"role": "user", "content": prompt}]
            )
            return comp.text
        except ReasoningPolicyDenied:
            # A D5 refusal (ADR 0035 clause 3) is a decision, not an outage: answering with the
            # placeholder below would turn "policy said no" into a successful-looking empty answer.
            raise
        except Exception:
            return prompt.split("Answer:")[-1].strip() or "(no answer)"

    def _retriever_span(self, question: str, hits: list, tenant: str) -> str | None:
        try:
            from examlops.telemetry import genai

            # A RETRIEVER span (ADR 0021 decision 1): ``retrieval`` is the GenAI operation and
            # OpenInference kind RETRIEVER, so Phoenix/Langfuse draw it as retrieval, not a tool.
            with genai.genai_span(
                "retrieval", system="rag", model="retriever", tenant=tenant
            ) as span:
                # The question is user content: it rides on the span only under the same gate
                # and redactor as every other prompt (EXAMLOPS_GENAI_CAPTURE_CONTENT, D8).
                genai.maybe_capture_content(span, prompt=question[:256])
                span.set_attribute("examlops.rag.doc_ids", [h.id for h in hits])
                span.set_attribute("examlops.rag.scores", [round(h.score, 4) for h in hits])
            return "recorded"
        except Exception:
            return None


def _rag_quality_via_provider(
    retrieved_ids: list[str], relevant_ids: list[str], *, provider: str | None
) -> dict[str, float] | None:
    """Recast the id-set inputs as the ``rag_quality`` provider's relevance-score shape.

    A binary "in the relevant set or not" is exactly a relevance score of 1.0/0.0 against the
    provider's default 0.5 threshold, so the default ``retrieval-lite`` provider reproduces
    ``context_precision``/``context_recall``'s own set-membership math to the 4 decimal places it
    declares (:class:`~examlops.llmops_providers.RetrievalLiteQualityProvider`) — the "default is
    the legacy path when unconfigured, a formal rounded contract when explicitly selected" rule
    (ADR 0074/0083), verified rather than assumed (see ``test_rag.py``).
    """
    from examlops.llmops_providers import rag_quality_via_provider

    rel = set(relevant_ids)
    relevances = [1.0 if d in rel else 0.0 for d in retrieved_ids]
    return rag_quality_via_provider(
        retrieved_relevances=relevances,
        relevant_total=len(relevant_ids),
        k=len(retrieved_ids) or 1,
        provider=provider,
    )


def context_precision(
    retrieved_ids: list[str], relevant_ids: list[str], *, provider: str | None = None
) -> float:
    """Fraction of retrieved chunks that are relevant (C2/Ragas-style, R5/GWT-4).

    Routes through the swappable ``rag_quality`` provider when one is configured
    (``EXAMLOPS_RAG_QUALITY_PROVIDER`` or a config ``provider:`` key, ADR 0083); unconfigured
    (the default) computes the exact set-membership fraction below, unchanged.
    """
    if not retrieved_ids:
        return 0.0
    try:
        via_provider = _rag_quality_via_provider(retrieved_ids, relevant_ids, provider=provider)
    except Exception:
        via_provider = None
    if via_provider is not None:
        return via_provider["precision"]
    rel = set(relevant_ids)
    return sum(1 for d in retrieved_ids if d in rel) / len(retrieved_ids)


def context_recall(
    retrieved_ids: list[str], relevant_ids: list[str], *, provider: str | None = None
) -> float:
    """Fraction of relevant chunks that were retrieved (R5/GWT-4). See ``context_precision``."""
    if not relevant_ids:
        return 0.0
    try:
        via_provider = _rag_quality_via_provider(retrieved_ids, relevant_ids, provider=provider)
    except Exception:
        via_provider = None
    if via_provider is not None:
        return via_provider["recall"]
    ret = set(retrieved_ids)
    return sum(1 for d in relevant_ids if d in ret) / len(relevant_ids)
