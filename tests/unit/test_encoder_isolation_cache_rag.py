"""ADR 0043 clause 2 for B3 and B4 — the cache and RAG stop mixing encoders.

Iteration 5 stamped the B5 vector store. These are the other two layers the clause names, and
the cache is the worst of the three: the vector store returns bad *ranking*, but the cache
returns a confident **wrong answer** with no model call in between to notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.platform_db import init_db  # noqa: E402
from examlops.semantic_cache import SemanticCache, namespace  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)
    init_db()


# ── B3 semantic cache ─────────────────────────────────────────────────────────


def test_an_entry_survives_within_one_encoder():
    c = SemanticCache(encoder_id="minilm@v1")
    c.store("what is drift?", "ANSWER-A", "gpt-4o", {})
    assert c.lookup("what is drift?", "gpt-4o", {})[0] == "ANSWER-A"


def test_changing_the_encoder_makes_old_entries_unreachable():
    """The failure this prevents: an entry embedded by one encoder and a query embedded by
    another are compared on axes with nothing to do with each other, and the result is not a
    miss — it is a similarity number that can clear the threshold by coincidence and return a
    cached answer to an unrelated question."""
    c = SemanticCache(encoder_id="minilm@v1")
    c.store("what is drift?", "ANSWER-A", "gpt-4o", {})
    c.encoder_id = "e5@v2"
    assert c.lookup("what is drift?", "gpt-4o", {})[0] is None


def test_the_encoder_change_is_a_miss_not_an_exception():
    """Routing, not erroring. A cache miss is a cache working correctly; an exception would be
    an outage caused by an upgrade."""
    c = SemanticCache(encoder_id="minilm@v1")
    c.store("q", "A", "gpt-4o", {})
    c.encoder_id = "e5@v2"
    completion, _sim = c.lookup("q", "gpt-4o", {})  # must not raise
    assert completion is None
    c.store("q", "B", "gpt-4o", {})
    assert c.lookup("q", "gpt-4o", {})[0] == "B", "the new encoder cannot populate the cache"


def test_both_encoders_can_coexist():
    """Old entries are invisible, not destroyed — a rollback finds its cache intact."""
    c = SemanticCache(encoder_id="minilm@v1")
    c.store("q", "OLD", "gpt-4o", {})
    c.encoder_id = "e5@v2"
    c.store("q", "NEW", "gpt-4o", {})
    assert c.lookup("q", "gpt-4o", {})[0] == "NEW"
    c.encoder_id = "minilm@v1"
    assert c.lookup("q", "gpt-4o", {})[0] == "OLD"


def test_an_unset_encoder_keeps_the_previous_namespace():
    """Additive: every caller that never heard of encoder ids keeps the key it had."""
    assert namespace("gpt-4o", {}, "default") == namespace("gpt-4o", {}, "default", None)
    assert "enc=" not in namespace("gpt-4o", {}, "default")


def test_the_encoder_does_not_replace_the_existing_isolation():
    """Tenant and params still separate entries — the encoder is another axis, not a substitute."""
    a = namespace("gpt-4o", {}, "t1", "enc")
    b = namespace("gpt-4o", {}, "t2", "enc")
    c = namespace("gpt-4o", {"temperature": 0.3}, "t1", "enc")
    assert len({a, b, c}) == 3


# ── B4 RAG ────────────────────────────────────────────────────────────────────


def test_rag_stamps_the_collection_with_the_encoder_it_ingested_with():
    """The encoder was already carried into `ingest` and written to `rag_kbs`, and never reached
    the store — so the B5 guard added for exactly this had nothing to compare against."""
    from examlops.rag import RagPipeline
    from examlops.vector_store import select_store

    RagPipeline().ingest(
        "kb1", [{"id": "d1", "text": "drift is a change in distribution"}], encoder="minilm@v1"
    )
    meta = select_store()._collection("kb1", "default")
    assert meta["encoder_id"] == "minilm@v1"


def test_a_query_against_a_kb_indexed_by_another_encoder_is_refused():
    """Retrieval is where cross-encoder scoring is most convincing and least detectable: every
    hit still arrives with a plausible score and a real citation attached."""
    from examlops.rag import RagPipeline
    from examlops.vector_store import EncoderMismatch, select_store

    rag = RagPipeline()
    rag.ingest(
        "kb2", [{"id": "d1", "text": "drift is a change in distribution"}], encoder="minilm@v1"
    )
    # The KB is re-recorded as having been built by a different encoder than the collection.
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute("UPDATE rag_kbs SET encoder='e5@v2' WHERE kb='kb2'")
    with pytest.raises(EncoderMismatch):
        rag.query("kb2", "what is drift?")
    assert select_store()._collection("kb2", "default")["encoder_id"] == "minilm@v1"


def test_a_kb_with_no_recorded_encoder_still_queries():
    """A corpus ingested before the stamp has nothing to compare against; refusing would break
    every one of them."""
    from examlops.data import get_db
    from examlops.rag import RagPipeline

    rag = RagPipeline()
    rag.ingest("kb3", [{"id": "d1", "text": "drift is a change in distribution"}])
    with get_db() as conn:
        conn.execute("UPDATE rag_kbs SET encoder=NULL WHERE kb='kb3'")
    assert rag._kb_encoder("kb3", "default") is None
    rag.query("kb3", "what is drift?")  # must not raise


def test_a_matching_encoder_queries_normally():
    from examlops.rag import RagPipeline

    rag = RagPipeline()
    rag.ingest(
        "kb4", [{"id": "d1", "text": "drift is a change in distribution"}], encoder="minilm@v1"
    )
    answer = rag.query("kb4", "what is drift?")
    assert answer.citations
