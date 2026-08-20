"""Knowledge / Docs-RAG memory tier (T2, Phase 3, ADR 0101).

Gives Skipper grounded "how do I …?" answers by chunking + embedding the documentation
(``docs/**``, ``design/adr/**``, …) into a vector collection, then retrieving the most relevant
chunks at query time. It **reuses the platform's own vector-store seam**
(:func:`examlops.vector_store.select_store` — the tenant-isolated SQLite fallback in ``platform.db``,
or pgvector) driven by **Skipper's local embeddings** (:func:`skipper.memory.build_embeddings`,
768-dim by default), and the pure RAG helpers (:func:`examlops.rag.chunk_text`,
:func:`examlops.rag._apply_guardrail` for prompt-injection defang of retrieved text).

Crucially it does **not** instantiate ``examlops.rag.RagPipeline`` — that pipeline hardcodes a
64-dim token-hash embedding and would raise ``DimensionMismatch`` against Skipper's 768-dim vectors.

Everything degrades gracefully: with no embeddings or no vector store, :func:`query` returns
``None`` and the caller (the ``search_knowledge`` tool) falls back to the existing ripgrep docs
tool — never worse than today.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from skipper import config

log = logging.getLogger("skipper.knowledge")


@dataclass
class Chunk:
    path: str
    text: str
    score: float = 0.0


# ── lazy, memoized reuse of the platform seam + agent embeddings ──────────────

# The embedding backend probes Ollama once; memoize so a chat turn doesn't re-probe per query.
# A sentinel distinguishes "not built yet" from "built and unavailable (None)".
_UNSET = object()
_EMBED_CACHE: object = _UNSET


def _embed():
    """Return Skipper's local embedding callable ``(texts)->vectors`` or ``None`` (memoized)."""
    global _EMBED_CACHE
    if _EMBED_CACHE is _UNSET:
        from skipper.memory import build_embeddings

        _EMBED_CACHE = build_embeddings()
    return _EMBED_CACHE


def reset_cache() -> None:
    """Clear the memoized embedder (for tests / after an embed-backend change)."""
    global _EMBED_CACHE
    _EMBED_CACHE = _UNSET


def _store():
    """Return the platform vector store, or ``None`` if the package is unavailable."""
    try:
        from examlops.vector_store import select_store

        return select_store()
    except Exception as exc:  # noqa: BLE001 - degrade to ripgrep docs
        log.warning("vector store unavailable (%s) — knowledge tier disabled", exc)
        return None


def _roots() -> list[Path]:
    roots = []
    for raw in config.AGENT_KNOWLEDGE_ROOTS.split(";"):
        raw = raw.strip()
        if raw and Path(raw).exists():
            roots.append(Path(raw))
    return roots


def available() -> bool:
    """True when the knowledge tier can actually run (enabled + store importable)."""
    return bool(config.AGENT_KNOWLEDGE_ENABLED and _store() is not None)


# ── ingest ────────────────────────────────────────────────────────────────────


def ingest(roots: list[str] | None = None) -> dict[str, int]:
    """Chunk + embed every Markdown file under the configured roots into the KB collection.

    Idempotent: re-ingesting upserts by a deterministic ``item_id`` (path + chunk index).
    Audited as an ``agent-knowledge`` event (best-effort).

    Returns ``{"files": n, "chunks": m}`` on success. When nothing could be indexed the result
    also carries the reason, so a caller can tell a deliberate ``disabled`` apart from a missing
    dependency (``unavailable`` plus ``no_embeddings`` / ``no_store``) — the CLI turns the second
    into a non-zero exit, because an ingest that indexed nothing must not look like a success.
    """
    if not config.AGENT_KNOWLEDGE_ENABLED:
        return {"files": 0, "chunks": 0, "disabled": 1}
    embed = _embed()
    store = _store()
    if embed is None or store is None:
        # Name the missing half. "unavailable" alone sent the operator looking at the vector
        # store when it is almost always the embedding backend that is not running, and the
        # caller cannot re-probe cheaply enough to work it out itself.
        return {
            "files": 0,
            "chunks": 0,
            "unavailable": 1,
            "no_embeddings": int(embed is None),
            "no_store": int(store is None),
        }

    from examlops.rag import chunk_text
    from examlops.vector_store import VecItem

    paths = [Path(r) for r in roots] if roots else _roots()
    kb = config.AGENT_KNOWLEDGE_KB
    store.create_collection(kb, config.AGENT_EMBED_DIMS, "cosine")

    files = 0
    total_chunks = 0
    for root in paths:
        md_files = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for md in md_files:
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            chunks = chunk_text(
                text, size=config.AGENT_KNOWLEDGE_CHUNK_SIZE, overlap=config.AGENT_KNOWLEDGE_OVERLAP
            )
            if not chunks:
                continue
            vectors = embed(chunks)
            items = [
                VecItem(
                    id=f"{md}::{i}",
                    vector=vectors[i],
                    metadata={"path": str(md), "chunk": i, "text": chunks[i]},
                )
                for i in range(len(chunks))
            ]
            store.upsert(kb, items)
            files += 1
            total_chunks += len(chunks)

    _audit_ingest(files, total_chunks)
    return {"files": files, "chunks": total_chunks}


def _audit_ingest(files: int, chunks: int) -> None:
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or config.AGENT_ACTOR
        write_audit_event(
            source="agent-knowledge",
            actor=actor,
            action="knowledge_ingest",
            target=config.AGENT_KNOWLEDGE_KB,
            details={"files": files, "chunks": chunks},
        )
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


# ── query ─────────────────────────────────────────────────────────────────────


def query(question: str, k: int = 5) -> list[Chunk] | None:
    """Return the top-``k`` documentation chunks for ``question``, or ``None`` to signal fallback.

    ``None`` means the knowledge tier is unavailable (no embeddings / no store / empty KB) — the
    caller should fall back to the ripgrep docs tool. Retrieved text passes the RAG guardrail
    (prompt-injection defang) before it is returned for inclusion in a prompt.
    """
    if not config.AGENT_KNOWLEDGE_ENABLED:
        return None
    embed = _embed()
    store = _store()
    if embed is None or store is None:
        return None
    try:
        from examlops.rag import _apply_guardrail
        from examlops.vector_store import CollectionNotFound

        qvec = embed([question])[0]
        try:
            hits = store.search(config.AGENT_KNOWLEDGE_KB, qvec, k=k)
        except CollectionNotFound:
            return None  # not ingested yet → fall back
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge query failed (%s) — falling back", exc)
        return None

    out: list[Chunk] = []
    for h in hits:
        md = h.metadata or {}
        _flagged, safe = _apply_guardrail(str(md.get("text", "")))
        out.append(Chunk(path=str(md.get("path", "?")), text=safe, score=float(h.score)))
    return out or None


# ── module CLI: `python -m skipper.knowledge {ingest|query} …` ────────────────


def _report_ingest(result: dict[str, int], roots: list[str] | None) -> int:
    """Print a human summary of an ingest and return the process exit code.

    An ingest that indexed nothing used to print a raw dict and exit 0, so
    ``make skipper-knowledge-ingest`` reported success while the knowledge tier stayed empty and
    Skipper went on answering ungrounded — the one failure mode nobody would notice from the
    outside. Only a deliberate switch-off is a success now; a missing dependency or an empty set
    of roots exits 1 and says which it was.
    """
    if result.get("disabled"):
        print("knowledge tier is switched off (AGENT_KNOWLEDGE_ENABLED=0) — nothing ingested")
        return 0
    if result.get("unavailable"):
        missing = []
        if result.get("no_embeddings"):
            missing.append(
                f"embeddings (AGENT_EMBED_BACKEND={config.AGENT_EMBED_BACKEND!r} is not reachable "
                "- start Ollama, or set AGENT_EMBED_BACKEND=sentence-transformers to run offline)"
            )
        if result.get("no_store"):
            missing.append("vector store (the examlops package is not importable here)")
        print("nothing ingested - the knowledge tier is unavailable:")
        for m in missing or ["reason unknown"]:
            print(f"  - {m}")
        print("Skipper falls back to the ripgrep docs tool, i.e. answers are not doc-grounded.")
        return 1
    files, chunks = result.get("files", 0), result.get("chunks", 0)
    if not files:
        where = ", ".join(roots) if roots else config.AGENT_KNOWLEDGE_ROOTS
        print(f"nothing ingested - no Markdown found under: {where}")
        return 1
    print(f"ingested {files} files / {chunks} chunks into collection {config.AGENT_KNOWLEDGE_KB!r}")
    return 0


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m skipper.knowledge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="Chunk + embed the docs roots into the KB collection")
    ing.add_argument("--roots", nargs="*", help="Override roots (default: AGENT_KNOWLEDGE_ROOTS)")
    q = sub.add_parser("query", help="Retrieve doc chunks for a question")
    q.add_argument("question")
    q.add_argument("-k", type=int, default=5)
    args = parser.parse_args(argv)

    if args.cmd == "ingest":
        return _report_ingest(ingest(args.roots), args.roots)
    hits = query(args.question, k=args.k)
    if not hits:
        print("(knowledge unavailable or empty — use ripgrep docs)")
        return 1
    for h in hits:
        print(f"\n[{h.score:.3f}] {h.path}\n  {h.text[:280]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
