# tests/unit/test_vector_hybrid.py
"""ADR 0020 clause 2 — hybrid dense+sparse search and ANN index configuration.

Pure maths first (BM25 against a hand-computed value, RRF and convex fusion), then the behaviour
the clause exists for on the dependency-free SQLite store: a chunk that names an exact identifier
the embedding cannot see is found by hybrid search and missed by dense search.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import vector_store as vs  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402
from examlops.vector_store import fusion, sparse  # noqa: E402
from examlops.vector_store.index import IndexConfig, IndexConfigError  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_VECTOR_BACKEND", raising=False)
    init_db()


# ── BM25 ──────────────────────────────────────────────────────────────────────


def test_bm25_matches_hand_computed_value():
    # N=3, df(banana)=1 → idf = ln(1 + 2.5/1.5); avgdl = 4/3; |d1| = 2
    # norm = 1.2·(0.25 + 0.75·2/(4/3)) = 1.65 ; score = idf · 2.2 / 2.65
    docs = [("d1", "apple banana"), ("d2", "apple"), ("d3", "cherry")]
    scores = sparse.bm25_scores("banana", docs)
    idf = math.log(1 + 2.5 / 1.5)
    assert scores == {"d1": pytest.approx(idf * 2.2 / 2.65)}


def test_bm25_idf_stays_positive_for_a_term_in_every_document():
    # The textbook RSJ idf is negative here; a negative weight would rank a document LOWER for
    # containing the query term. The Lucene form must keep it positive.
    scores = sparse.bm25_scores("apple", [("a", "apple"), ("b", "apple pie"), ("c", "apple x")])
    assert set(scores) == {"a", "b", "c"}
    assert all(s > 0 for s in scores.values())
    assert scores["a"] > scores["b"]  # the shorter document is the denser match


def test_bm25_omits_non_matching_and_handles_empty_input():
    assert sparse.bm25_scores("zzz", [("a", "apple")]) == {}
    assert sparse.bm25_scores("", [("a", "apple")]) == {}
    assert sparse.bm25_scores("apple", []) == {}
    # documents without text count toward N but never match
    assert set(sparse.bm25_scores("apple", [("a", "apple"), ("b", None)])) == {"a"}


def test_tokenizer_keeps_identifiers_whole_and_tsquery_is_operator_free():
    assert sparse.tokenize("JPCP job-4711 PM100Dataset!") == ["jpcp", "job", "4711", "pm100dataset"]
    q = sparse.tsquery_or("a & b | !c (d):* a")
    assert q == "a | b | c | d"  # deduplicated, no tsquery operators survive
    assert sparse.tsquery_or("  ") == ""


# ── fusion ────────────────────────────────────────────────────────────────────


def test_rrf_matches_the_formula():
    fused = fusion.rrf({"dense": ["a", "b"], "sparse": ["b", "c"]}, k=60)
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["c"] == pytest.approx(1 / 62)
    assert fusion.ranked(fused) == ["b", "a", "c"]
    with pytest.raises(ValueError):
        fusion.rrf({"x": ["a"]}, k=0)


def test_ranked_breaks_ties_by_id_deterministically():
    assert fusion.ranked({"b": 1.0, "a": 1.0, "c": 2.0}) == ["c", "a", "b"]


def test_convex_endpoints_and_normalisation():
    dense = {"a": 0.9, "b": 0.1}
    sp = {"b": 7.0, "c": 3.0}
    assert fusion.ranked(fusion.convex(dense, sp, alpha=1.0))[0] == "a"
    assert fusion.ranked(fusion.convex(dense, sp, alpha=0.0))[0] == "b"
    mixed = fusion.convex(dense, sp, alpha=0.5)
    assert mixed["b"] == pytest.approx(0.5 * 0.0 + 0.5 * 1.0)
    # a single-candidate channel normalises to 1.0, not 0.0
    assert fusion.convex({}, {"only": 2.0}, alpha=0.0) == {"only": 1.0}
    with pytest.raises(ValueError):
        fusion.convex(dense, sp, alpha=1.5)
    with pytest.raises(ValueError):
        fusion.fuse(dense, sp, method="borda")


# ── index configuration ───────────────────────────────────────────────────────


def test_index_config_defaults_and_validation():
    assert IndexConfig.build("hnsw").params == {"m": 16, "ef_construction": 64, "ef_search": 40}
    assert IndexConfig.build("ivfflat", lists=200).params == {"lists": 200, "probes": 1}
    assert IndexConfig.build("flat").params == {}
    with pytest.raises(IndexConfigError, match="2\\*m"):
        IndexConfig.build("hnsw", m=48, ef_construction=64)
    with pytest.raises(IndexConfigError, match="do not apply"):
        IndexConfig.build("hnsw", lists=10)
    with pytest.raises(IndexConfigError, match="at most 2000"):
        IndexConfig.build("hnsw", dim=3072)
    with pytest.raises(IndexConfigError):
        IndexConfig.build("annoy")
    with pytest.raises(IndexConfigError):
        IndexConfig.build("ivfflat", lists=10, probes=11)
    assert IndexConfig.build("flat", dim=3072).type == "flat"  # exact scans have no dim cap


def test_index_config_round_trips_through_a_row():
    cfg = IndexConfig.build("hnsw", m=24, ef_search=100)
    assert IndexConfig.from_row(cfg.type, cfg.to_json()) == cfg
    assert IndexConfig.from_row(None, None) == IndexConfig()  # legacy row = flat


# ── the SQLite store ──────────────────────────────────────────────────────────


def _corpus(store: vs.SqliteVectorStore, tenant: str = "acme") -> None:
    """Five chunks. The query vector points at the 'semantic' cluster; the identifier the
    question names lives only in `incident`, whose vector points elsewhere."""
    store.create_collection("kb", 3, "cosine", tenant)
    store.upsert(
        "kb",
        [
            vs.VecItem(
                "s1", [1.0, 0.0, 0.0], {"lang": "en"}, "training jobs fail when memory runs out"
            ),
            vs.VecItem("s2", [0.95, 0.1, 0.0], {"lang": "en"}, "a failed training job is retried"),
            vs.VecItem("s3", [0.9, 0.2, 0.0], {"lang": "it"}, "il job di training è fallito"),
            vs.VecItem("s4", [0.85, 0.3, 0.0], {"lang": "en"}, "jobs are scheduled by flux"),
            vs.VecItem("incident", [0.0, 0.0, 1.0], {"lang": "en"}, "JPCP-4711 crashed on gpu01"),
        ],
        tenant,
    )


def test_hybrid_finds_the_identifier_dense_search_misses():
    s = vs.SqliteVectorStore()
    _corpus(s)
    q = [1.0, 0.05, 0.0]
    dense = [h.id for h in s.search("kb", q, 3, None, "acme")]
    assert "incident" not in dense
    hybrid = s.hybrid_search("kb", q, "why did JPCP-4711 fail", 3, None, "acme")
    ids = [h.id for h in hybrid]
    assert "incident" in ids
    inc = next(h for h in hybrid if h.id == "incident")
    assert inc.channels["sparse_rank"] == 1.0  # the keyword carried it
    assert inc.channels["dense_rank"] == 5.0  # and the embedding ranked it last


def test_hybrid_with_convex_fusion_and_alpha_one_is_dense():
    s = vs.SqliteVectorStore()
    _corpus(s)
    q = [1.0, 0.05, 0.0]
    dense = [h.id for h in s.search("kb", q, 5, None, "acme")]
    convex = s.hybrid_search("kb", q, "JPCP-4711", 5, None, "acme", fusion="convex", alpha=1.0)
    assert [h.id for h in convex] == dense


def test_hybrid_with_no_query_text_degrades_to_the_dense_order():
    s = vs.SqliteVectorStore()
    _corpus(s)
    q = [1.0, 0.05, 0.0]
    assert [h.id for h in s.hybrid_search("kb", q, "", 5, None, "acme")] == [
        h.id for h in s.search("kb", q, 5, None, "acme")
    ]


def test_sparse_search_ranks_by_bm25_and_filters_before_scoring():
    s = vs.SqliteVectorStore()
    _corpus(s)
    assert [h.id for h in s.sparse_search("kb", "training job", 2, None, "acme")][0] in {"s1", "s2"}
    it_only = s.sparse_search("kb", "training job", 5, {"lang": "it"}, "acme")
    assert [h.id for h in it_only] == ["s3"]
    hybrid_it = s.hybrid_search("kb", [1.0, 0.0, 0.0], "training", 5, {"lang": "it"}, "acme")
    assert [h.id for h in hybrid_it] == ["s3"]


def test_hybrid_is_tenant_isolated_and_encoder_guarded():
    s = vs.SqliteVectorStore()
    _corpus(s, tenant="acme")
    with pytest.raises(vs.CollectionNotFound):
        s.hybrid_search("kb", [1.0, 0.0, 0.0], "JPCP", 3, None, "other")
    with pytest.raises(vs.CollectionNotFound):
        s.sparse_search("kb", "JPCP", 3, None, "other")
    s.create_collection("stamped", 3, "cosine", "acme", encoder_id="minilm@v1")
    s.upsert("stamped", [vs.VecItem("x", [1.0, 0.0, 0.0], text="hello")], "acme", "minilm@v1")
    with pytest.raises(vs.EncoderMismatch):
        s.hybrid_search("stamped", [1.0, 0.0, 0.0], "hello", 1, None, "acme", encoder_id="e5@v2")


def test_rows_written_before_the_text_column_fall_back_to_metadata_text():
    s = vs.SqliteVectorStore()
    s.create_collection("legacy", 2, "cosine", "acme")
    s.upsert("legacy", [vs.VecItem("old", [1.0, 0.0], {"text": "pm100 dataset card"})], "acme")
    with get_db() as conn:  # simulate a pre-migration row
        conn.execute("UPDATE vector_items SET text = NULL WHERE item_id = 'old'")
    assert [h.id for h in s.sparse_search("legacy", "pm100", 1, None, "acme")] == ["old"]


def test_collection_carries_its_index_and_reports_exact_search():
    s = vs.SqliteVectorStore()
    s.create_collection("c", 8, "cosine", "acme", index=IndexConfig.build("hnsw", m=8))
    st = s.stats("c", "acme")
    assert st["index"] == {"type": "hnsw", "m": 8, "ef_construction": 64, "ef_search": 40}
    assert st["search"] == "exact" and st["backend"] == "sqlite"
    s.reindex("c", "acme", index=IndexConfig.build("ivfflat", lists=10))
    assert s.stats("c", "acme")["index"]["type"] == "ivfflat"
    with pytest.raises(IndexConfigError):
        s.create_collection("big", 4096, "cosine", "acme", index=IndexConfig.build("hnsw"))


def test_recreate_keeps_stamp_and_index_and_refuses_schema_change_under_data():
    s = vs.SqliteVectorStore()
    s.create_collection("c", 2, "cosine", "acme", encoder_id="m@1", index=IndexConfig.build("hnsw"))
    s.create_collection("c", 2, "cosine", "acme")  # identical re-declare: idempotent
    st = s.stats("c", "acme")
    assert st["encoder_id"] == "m@1" and st["index"]["type"] == "hnsw"
    s.upsert("c", [vs.VecItem("x", [1.0, 0.0])], "acme")
    with pytest.raises(vs.SchemaConflict, match="dim 2 → 3"):
        s.create_collection("c", 3, "cosine", "acme")
    with pytest.raises(vs.SchemaConflict, match="encoder"):
        s.create_collection("c", 2, "cosine", "acme", encoder_id="other@2")
    # an empty collection may change shape
    s.create_collection("empty", 2, "cosine", "acme")
    s.create_collection("empty", 5, "l2", "acme")
    assert s.stats("empty", "acme")["dim"] == 5


def test_drop_removes_items_and_keeps_the_metrics_history():
    s = vs.SqliteVectorStore()
    _corpus(s)
    assert s.drop_collection("kb", "acme") == 5
    with pytest.raises(vs.CollectionNotFound):
        s.stats("kb", "acme")
    with get_db() as conn:
        ops = [r["operation"] for r in conn.execute("SELECT operation FROM vector_metrics")]
        left = conn.execute("SELECT COUNT(*) AS n FROM vector_items").fetchone()["n"]
    assert "drop" in ops and "upsert" in ops and left == 0


def test_non_finite_vectors_are_refused():
    s = vs.SqliteVectorStore()
    s.create_collection("c", 2, "cosine", "acme")
    with pytest.raises(ValueError, match="NaN"):
        s.upsert("c", [vs.VecItem("bad", [float("nan"), 1.0])], "acme")


def test_select_store_refuses_an_unknown_backend(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VECTOR_BACKEND", "pgvektor")
    with pytest.raises(ValueError, match="unknown vector backend"):
        vs.select_store()
    monkeypatch.setenv("EXAMLOPS_VECTOR_BACKEND", "pgvector")
    monkeypatch.delenv("EXAMLOPS_PGVECTOR_DSN", raising=False)
    with pytest.raises(RuntimeError, match="EXAMLOPS_PGVECTOR_DSN"):
        vs.select_store()


def test_rag_hybrid_retrieval_cites_the_identifier_chunk():
    from examlops.rag import RagPipeline

    docs = [
        {"id": "guide", "text": "training jobs are retried when they fail on the cluster"},
        {"id": "guide2", "text": "failed training jobs report their exit code to the scheduler"},
        {"id": "incident", "text": "incident JPCP-4711 root cause was an out of memory kill"},
    ]
    RagPipeline().ingest("kb", docs)
    q = "what happened to JPCP-4711"

    def ids(pipeline: RagPipeline) -> list[str]:
        ans = pipeline.query("kb", q, k=1, generate_fn=lambda p: "ok")
        return [c.doc_id.split("#")[0] for c in ans.citations]

    assert ids(RagPipeline(retrieval="hybrid", reranker=lambda _q, h: h)) == ["incident"]
    with pytest.raises(ValueError):
        RagPipeline(retrieval="bm25")


def test_pgvector_schema_must_be_a_plain_identifier():
    # The schema is spliced into a libpq options string; a space would smuggle in a `-c`.
    with pytest.raises(ValueError, match="plain identifier"):
        vs.PgVectorStore("postgresql://u@127.0.0.1:1/db", schema="a -c log_statement=all")
