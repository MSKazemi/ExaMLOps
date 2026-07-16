# tests/unit/test_vector_store.py
"""B5 — Vector DB & embedding store (ADR 0020, spec B5).

GWT-1 nearest search · GWT-2 dim enforcement · GWT-3 metadata filter ·
GWT-4 tenant isolation · GWT-5 reindex preserves recall.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import vector_store as vs  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()


def _store():
    return vs.SqliteVectorStore()


def test_gwt1_nearest_search():
    s = _store()
    s.create_collection("c", 3, "cosine", "acme")
    s.upsert(
        "c",
        [
            vs.VecItem("near", [1.0, 0.0, 0.0]),
            vs.VecItem("mid", [0.7, 0.7, 0.0]),
            vs.VecItem("far", [0.0, 0.0, 1.0]),
        ],
        "acme",
    )
    hits = s.search("c", [1.0, 0.0, 0.0], k=2, flt=None, tenant="acme")
    assert [h.id for h in hits] == ["near", "mid"]
    assert hits[0].score == pytest.approx(1.0)


def test_gwt2_dim_enforcement():
    s = _store()
    s.create_collection("c", 384, "cosine", "acme")
    with pytest.raises(vs.DimensionMismatch):
        s.upsert("c", [vs.VecItem("bad", [0.1, 0.2, 0.3])], "acme")


def test_search_dim_mismatch_rejected():
    s = _store()
    s.create_collection("c", 3, "cosine", "acme")
    with pytest.raises(vs.DimensionMismatch):
        s.search("c", [0.1, 0.2], 5, None, "acme")


def test_gwt3_metadata_filter():
    s = _store()
    s.create_collection("c", 2, "cosine", "acme")
    s.upsert(
        "c",
        [
            vs.VecItem("en1", [1.0, 0.0], {"lang": "en"}),
            vs.VecItem("it1", [1.0, 0.0], {"lang": "it"}),
        ],
        "acme",
    )
    hits = s.search("c", [1.0, 0.0], k=5, flt={"lang": "it"}, tenant="acme")
    assert [h.id for h in hits] == ["it1"]


def test_gwt4_tenant_isolation():
    s = _store()
    s.create_collection("c", 2, "cosine", "tenantA")
    s.upsert("c", [vs.VecItem("a1", [1.0, 0.0])], "tenantA")
    # tenant B has no such collection → CollectionNotFound
    with pytest.raises(vs.CollectionNotFound):
        s.search("c", [1.0, 0.0], 5, None, "tenantB")


def test_gwt5_reindex_preserves_recall():
    s = _store()
    s.create_collection("c", 2, "cosine", "acme")
    s.upsert("c", [vs.VecItem("x", [1.0, 0.0]), vs.VecItem("y", [0.0, 1.0])], "acme")
    before = {h.id for h in s.search("c", [1.0, 0.0], 5, None, "acme")}
    s.reindex("c", "acme")
    after = {h.id for h in s.search("c", [1.0, 0.0], 5, None, "acme")}
    assert before == after  # recall preserved, search still available


def test_l2_metric_ranks_by_distance():
    s = _store()
    s.create_collection("c", 2, "l2", "acme")
    s.upsert("c", [vs.VecItem("close", [1.0, 1.0]), vs.VecItem("far", [9.0, 9.0])], "acme")
    hits = s.search("c", [1.1, 1.1], k=2, flt=None, tenant="acme")
    assert hits[0].id == "close"


def test_upsert_updates_existing():
    s = _store()
    s.create_collection("c", 2, "cosine", "acme")
    s.upsert("c", [vs.VecItem("x", [1.0, 0.0], {"v": 1})], "acme")
    s.upsert("c", [vs.VecItem("x", [0.0, 1.0], {"v": 2})], "acme")
    assert s.count("c", "acme") == 1
    hit = s.search("c", [0.0, 1.0], 1, None, "acme")[0]
    assert hit.metadata["v"] == 2


def test_stats_and_metrics_recorded():
    s = _store()
    s.create_collection("c", 2, "cosine", "acme")
    s.upsert("c", [vs.VecItem("x", [1.0, 0.0])], "acme")
    s.search("c", [1.0, 0.0], 1, None, "acme")
    st = s.stats("c", "acme")
    assert st["count"] == 1 and st["dim"] == 2
    from examlops.platform_db import get_db

    with get_db() as conn:
        ops = {r["operation"] for r in conn.execute("SELECT operation FROM vector_metrics")}
    assert {"upsert", "search"} <= ops


def test_select_store_default_is_sqlite(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_VECTOR_BACKEND", raising=False)
    store = vs.select_store()
    assert store.name == "sqlite"
    assert isinstance(store, vs.VectorStore)


def test_invalid_metric_rejected():
    s = _store()
    with pytest.raises(ValueError):
        s.create_collection("c", 2, "manhattan", "acme")
