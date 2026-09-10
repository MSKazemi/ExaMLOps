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
    """Split text into overlapping word-window chunks (R1)."""
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    step = max(1, size - overlap)
    return [
        " ".join(words[i : i + size]) for i in range(0, len(words), step) if words[i : i + size]
    ]


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

    def __post_init__(self) -> None:
        if self.retrieval not in RETRIEVAL_MODES:
            raise ValueError(f"retrieval '{self.retrieval}' not in {RETRIEVAL_MODES}")

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
        encoder: str = "token-hash",
    ) -> int:
        """Chunk→embed→index documents into the B5 store; version against A1 (R1)."""
        from examlops.data import get_db, init_db
        from examlops.vector_store import VecItem

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
                chunk_text(doc["text"], self.chunk_size, self.chunk_overlap)
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
        return len(items)

    def query(
        self,
        kb: str,
        question: str,
        *,
        tenant: str = "default",
        k: int = 5,
        generate_fn: Callable[[str], str] | None = None,
        prompt_label: str | None = None,
    ) -> RagAnswer:
        """embed→retrieve→rerank→assemble→generate, citing chunks (R2/R3/GWT-1)."""
        store = self._store()
        qv = self.embed_fn(question)
        # Ask under the encoder this KB was indexed with. If the query embedding comes from a
        # different one the store refuses rather than returning ranked nonsense — retrieval is
        # the case where cross-encoder scoring is most convincing and least detectable, because
        # every hit still arrives with a plausible score and a real citation attached.
        encoder_id = self._kb_encoder(kb, tenant)
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
        hits = self.reranker(question, hits)[:k]  # rerank then trim (GWT-3)

        span_id = self._retriever_span(question, hits, tenant)

        # D8 guardrail over untrusted retrieved content (R7/GWT-6).
        flagged = False
        contexts: list[str] = []
        for h in hits:
            f, safe = _apply_guardrail(str(h.metadata.get("text", "")))
            flagged = flagged or f
            contexts.append(safe)

        prompt = self._assemble_prompt(question, contexts, prompt_label)
        answer = (generate_fn or self._default_generate)(prompt)
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

    def _default_generate(self, prompt: str) -> str:
        try:
            from examlops.gateway import GatewayClient, build_default_router

            comp = GatewayClient(build_default_router()).chat(
                "default", [{"role": "user", "content": prompt}]
            )
            return comp.text
        except Exception:
            return prompt.split("Answer:")[-1].strip() or "(no answer)"

    def _retriever_span(self, question: str, hits: list, tenant: str) -> str | None:
        try:
            from examlops.telemetry import genai

            with genai.genai_span("tool", system="rag", model="retriever", tenant=tenant) as span:
                span.set_attribute("examlops.rag.query", question[:256])
                span.set_attribute("examlops.rag.doc_ids", [h.id for h in hits])
                span.set_attribute("examlops.rag.scores", [round(h.score, 4) for h in hits])
            return "recorded"
        except Exception:
            return None


def context_precision(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """Fraction of retrieved chunks that are relevant (C2/Ragas-style, R5/GWT-4)."""
    if not retrieved_ids:
        return 0.0
    rel = set(relevant_ids)
    return sum(1 for d in retrieved_ids if d in rel) / len(retrieved_ids)


def context_recall(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """Fraction of relevant chunks that were retrieved (R5/GWT-4)."""
    if not relevant_ids:
        return 0.0
    ret = set(retrieved_ids)
    return sum(1 for d in relevant_ids if d in ret) / len(relevant_ids)
