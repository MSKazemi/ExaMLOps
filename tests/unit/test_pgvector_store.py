# tests/unit/test_pgvector_store.py
"""ADR 0020 — the pgvector store against a real Postgres + pgvector.

Opt-in (``-m live``): set ``EXAMLOPS_PGVECTOR_TEST_DSN`` to a database where the role may
``CREATE EXTENSION vector`` (the ``pgvector/pgvector`` image works as-is)::

    docker run -d --name pgv -e POSTGRES_PASSWORD=vt -e POSTGRES_USER=vt -e POSTGRES_DB=vt \\
        -p 127.0.0.1:55433:5432 pgvector/pgvector:pg17
    EXAMLOPS_PGVECTOR_TEST_DSN=postgresql://vt:vt@127.0.0.1:55433/vt \\
        .venv/bin/pytest tests/unit/test_pgvector_store.py -m live

Every test runs in its own schema, dropped afterwards, so the suite is parallel-safe.

The central assertion is **parity**: the same data and query give the same ranking on pgvector
as on the SQLite fallback, for every metric, with and without a filter, in dense, sparse and
hybrid mode. A backend that answered differently would make the fallback a lie about production.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import vector_store as vs  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.vector_store.index import IndexConfig  # noqa: E402

DSN = os.getenv("EXAMLOPS_PGVECTOR_TEST_DSN")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not DSN, reason="EXAMLOPS_PGVECTOR_TEST_DSN not set"),
]


@pytest.fixture
def pg(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()
    schema = f"t_{uuid.uuid4().hex[:12]}"
    store = vs.PgVectorStore(DSN, schema=schema)
    yield store
    import psycopg

    with psycopg.connect(str(DSN), autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def lite(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()
    return vs.SqliteVectorStore()


_ITEMS = [
    vs.VecItem("s1", [1.0, 0.0, 0.0], {"lang": "en"}, "training jobs fail when memory runs out"),
    vs.VecItem("s2", [0.95, 0.1, 0.0], {"lang": "en"}, "a failed training job is retried"),
    vs.VecItem("s3", [0.9, 0.2, 0.05], {"lang": "it"}, "il job di training è fallito"),
    vs.VecItem("s4", [0.5, 0.8, 0.1], {"lang": "en"}, "jobs are scheduled by flux"),
    vs.VecItem("incident", [0.0, 0.1, 1.0], {"lang": "en"}, "JPCP-4711 crashed on gpu01"),
]
_Q = [1.0, 0.05, 0.0]


def _load(store, metric="cosine", index=None, tenant="acme"):
    store.create_collection("kb", 3, metric, tenant, index=index)
    store.upsert("kb", list(_ITEMS), tenant)


@pytest.mark.parametrize("metric", ["cosine", "l2", "dot"])
@pytest.mark.parametrize("flt", [None, {"lang": "en"}])
def test_dense_ranking_matches_the_sqlite_fallback(pg, lite, metric, flt):
    _load(pg, metric)
    _load(lite, metric)
    got = pg.search("kb", _Q, 5, flt, "acme")
    want = lite.search("kb", _Q, 5, flt, "acme")
    assert [h.id for h in got] == [h.id for h in want]
    for g, w in zip(got, want):
        assert g.score == pytest.approx(w.score, abs=1e-5)  # vector is float4 on pgvector


def test_hybrid_ranking_matches_and_finds_the_identifier(pg, lite):
    _load(pg)
    _load(lite)
    text = "why did JPCP-4711 fail"
    got = pg.hybrid_search("kb", _Q, text, 3, None, "acme")
    assert "incident" in [h.id for h in got]
    assert "incident" not in [h.id for h in pg.search("kb", _Q, 3, None, "acme")]
    # RRF uses ranks only; both backends agree on the lexical top-1 here, so the fused
    # membership agrees even though ts_rank_cd and BM25 differ in scale.
    want = lite.hybrid_search("kb", _Q, text, 3, None, "acme")
    assert {h.id for h in got} == {h.id for h in want}
    inc = next(h for h in got if h.id == "incident")
    assert inc.channels["sparse_rank"] == 1.0


def test_sparse_search_uses_the_text_index_and_filter(pg):
    _load(pg)
    assert [h.id for h in pg.sparse_search("kb", "JPCP-4711", 3, None, "acme")] == ["incident"]
    assert [h.id for h in pg.sparse_search("kb", "training", 5, {"lang": "it"}, "acme")] == ["s3"]
    assert pg.sparse_search("kb", "   ", 5, None, "acme") == []


@pytest.mark.parametrize(
    "index", [IndexConfig.build("hnsw", m=8, ef_construction=32), IndexConfig.build("flat")]
)
def test_ann_index_serves_and_is_reported(pg, index):
    _load(pg, index=index)
    st = pg.stats("kb", "acme")
    assert st["count"] == 5 and st["index"]["type"] == index.type
    assert st["search"] == ("ann" if index.type == "hnsw" else "exact")
    # a selective filter over an HNSW scan must still return the matching rows (iterative scan)
    assert [h.id for h in pg.search("kb", _Q, 5, {"lang": "it"}, "acme")] == ["s3"]


def test_ivfflat_is_built_by_reindex_not_on_an_empty_table(pg):
    _load(pg, index=IndexConfig.build("ivfflat", lists=2, probes=2))
    assert pg.stats("kb", "acme")["search"].startswith("exact (index declared but not built")
    pg.reindex("kb", "acme")
    assert pg.stats("kb", "acme")["search"] == "ann"
    assert len(pg.search("kb", _Q, 5, None, "acme")) == 5


def test_blue_green_reindex_switches_index_type_and_keeps_recall(pg):
    _load(pg, index=IndexConfig.build("hnsw"))
    before = [h.id for h in pg.search("kb", _Q, 5, None, "acme")]
    pg.reindex("kb", "acme", index=IndexConfig.build("ivfflat", lists=1))
    assert pg.stats("kb", "acme")["index"]["type"] == "ivfflat"
    assert [h.id for h in pg.search("kb", _Q, 5, None, "acme")] == before
    pg.reindex("kb", "acme", index=IndexConfig.build("flat"))
    assert pg.stats("kb", "acme")["search"] == "exact"
    assert [h.id for h in pg.search("kb", _Q, 5, None, "acme")] == before


def test_contract_guards_match_the_sqlite_store(pg):
    _load(pg)
    with pytest.raises(vs.DimensionMismatch):
        pg.upsert("kb", [vs.VecItem("bad", [1.0, 2.0])], "acme")
    with pytest.raises(vs.DimensionMismatch):
        pg.search("kb", [1.0, 2.0], 3, None, "acme")
    with pytest.raises(ValueError, match="NaN"):
        pg.upsert("kb", [vs.VecItem("nan", [float("nan"), 0.0, 0.0])], "acme")
    with pytest.raises(vs.SchemaConflict):
        pg.create_collection("kb", 4, "cosine", "acme")
    with pytest.raises(vs.CollectionNotFound):
        pg.search("kb", _Q, 3, None, "other-tenant")
    pg.create_collection("stamped", 3, "cosine", "acme", encoder_id="minilm@v1")
    pg.upsert("stamped", [vs.VecItem("x", [1.0, 0.0, 0.0])], "acme", encoder_id="minilm@v1")
    with pytest.raises(vs.EncoderMismatch):
        pg.search("stamped", _Q, 1, None, "acme", encoder_id="e5@v2")
    pg.create_collection("stamped", 3, "cosine", "acme")  # re-declare keeps the stamp
    assert pg.stats("stamped", "acme")["encoder_id"] == "minilm@v1"


def test_upsert_is_an_upsert_and_drop_erases_the_table(pg):
    _load(pg)
    pg.upsert("kb", [vs.VecItem("s1", [0.0, 0.0, 1.0], {"v": 2}, "moved")], "acme")
    assert pg.count("kb", "acme") == 5
    assert pg.search("kb", [0.0, 0.0, 1.0], 1, {"v": 2}, "acme")[0].id == "s1"
    assert pg.drop_collection("kb", "acme") == 5
    with pytest.raises(vs.CollectionNotFound):
        pg.stats("kb", "acme")
    _load(pg)  # the name is reusable after a drop
    assert pg.count("kb", "acme") == 5


def test_empty_collection_may_change_shape(pg):
    pg.create_collection("c", 3, "cosine", "acme")
    pg.create_collection("c", 5, "l2", "acme")
    pg.upsert("c", [vs.VecItem("a", [1.0, 0.0, 0.0, 0.0, 0.0])], "acme")
    assert pg.stats("c", "acme")["dim"] == 5
