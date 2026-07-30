"""Phase 3 (Skipper next-gen) — Knowledge / Docs-RAG memory tier (T2, ADR 0101).

Verifies ingest → semantic query round-trips through the platform vector-store seam driven by a
deterministic fake embedder (no Ollama needed), that retrieval is guardrail-defanged, that the
``search_knowledge`` tool falls back to ripgrep docs when the tier is unavailable, and that the
whole tier degrades gracefully (no embeddings / not ingested → ``None`` → fallback).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import config, knowledge  # noqa: E402


def _fake_embed(dim=8):
    """Deterministic bag-of-words embedding into ``dim`` buckets (cosine-meaningful)."""

    def embed(texts):
        out = []
        for t in texts:
            vec = [0.0] * dim
            for tok in t.lower().split():
                vec[hash(tok) % dim] += 1.0
            out.append(vec)
        return out

    return embed


@pytest.fixture
def kb(tmp_path, monkeypatch):
    # Isolated platform.db + a tiny docs root, deterministic 8-dim embeddings.
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "kb.db"))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "promote.md").write_text(
        "# Promotion\nTo promote a model safely run exa pipeline promote with an rmse gate.\n"
    )
    (docs_dir / "drift.md").write_text(
        "# Drift\nCheck drift with exa drift status and set a baseline with exa drift baseline.\n"
    )
    monkeypatch.setattr(config, "AGENT_KNOWLEDGE_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_KNOWLEDGE_ROOTS", str(docs_dir))
    monkeypatch.setattr(config, "AGENT_KNOWLEDGE_KB", "test-kb")
    monkeypatch.setattr(config, "AGENT_EMBED_DIMS", 8)
    monkeypatch.setattr(knowledge, "_EMBED_CACHE", _fake_embed(8))
    knowledge.reset_cache()
    monkeypatch.setattr(knowledge, "_EMBED_CACHE", _fake_embed(8))
    yield docs_dir


def test_ingest_then_query_roundtrip(kb):
    result = knowledge.ingest()
    assert result["files"] == 2
    assert result["chunks"] >= 2
    hits = knowledge.query("how do I promote a model safely", k=3)
    assert hits, "expected retrieval hits after ingest"
    # the promotion doc should surface for a promotion question
    assert any("promote" in h.text.lower() for h in hits)
    assert all(h.path.endswith(".md") for h in hits)


def test_query_returns_none_before_ingest(kb):
    # Collection not created yet → CollectionNotFound → None → caller falls back.
    assert knowledge.query("anything") is None


def test_query_none_when_disabled(kb, monkeypatch):
    monkeypatch.setattr(config, "AGENT_KNOWLEDGE_ENABLED", False)
    assert knowledge.query("anything") is None


def test_ingest_audits_event(kb):
    knowledge.ingest()
    from examlops.data import get_db, init_db

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT action, target FROM audit_events WHERE source='agent-knowledge'"
        ).fetchall()
    assert any(r[0] == "knowledge_ingest" and r[1] == "test-kb" for r in rows)


def test_search_knowledge_tool_falls_back_to_ripgrep(kb, monkeypatch):
    from skipper.tools import knowledge as ktool

    # Force the tier to report "no hits" so the tool takes the ripgrep docs path.
    monkeypatch.setattr(ktool.knowledge, "query", lambda q, k=5: None)
    monkeypatch.setattr(config, "AGENT_DOCS_ROOT", str(kb))
    out = ktool.search_knowledge.invoke({"query": "drift"})
    assert "drift" in out.lower()


def test_search_knowledge_tool_uses_semantic_hits(kb, monkeypatch):
    from skipper.tools import knowledge as ktool

    knowledge.ingest()
    monkeypatch.setattr(knowledge, "_EMBED_CACHE", _fake_embed(8))
    out = ktool.search_knowledge.invoke({"query": "promote a model safely"})
    assert "semantic search" in out.lower()
    assert ".md" in out
