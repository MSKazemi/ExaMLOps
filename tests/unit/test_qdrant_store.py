# tests/unit/test_qdrant_store.py
"""ADR 0020 clause 1 — the Qdrant scale-out store behind the `VectorStore` seam.

Two suites in one file, because they check two different things about the same adapter.

**The contract suite** drives `QdrantVectorStore` against an in-process double of
`qdrant-client` (`FakeQdrantClient` + `fake_models` below) and owns the whole protocol: create,
upsert, dense/sparse/hybrid search, metadata filters, tenant isolation, schema and encoder
refusals, reindex, trim, scan, drop, stats, metrics, and the missing-package error. It runs
everywhere and needs no server, so the adapter's *logic* is guarded in the ordinary unit run.

The double is not a stub that says yes. It mirrors the behaviour this adapter depends on and was
checked against a real Qdrant 1.x on 2026-09-24: Cosine normalises vectors on write and scores by
similarity, Euclid scores by **distance** (ascending), a `MatchText` condition is a server error
without a full-text payload index, `order_by` needs the ordered field indexed, and a point id
must be a UUID or an unsigned integer — never an arbitrary string. Each of those is a bug this
adapter could have shipped, so each is enforced here.

**The live suite** (`-m live`, `make qdrant-live`) runs the same claims against a real Qdrant and
adds the one thing a double can never prove: that the adapter's ranking **matches the SQLite
fallback's** for every metric, filtered and unfiltered, dense and hybrid. A backend that answered
differently would make the fallback a lie about production.

    docker run -d --name qd -p 127.0.0.1:56333:6333 qdrant/qdrant
    EXAMLOPS_QDRANT_TEST_URL=http://127.0.0.1:56333 \\
        .venv/bin/pytest tests/unit/test_qdrant_store.py -m live
"""

from __future__ import annotations

import math
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import vector_store as vs  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.vector_store.index import IndexConfig  # noqa: E402
from examlops.vector_store.qdrant import (  # noqa: E402
    QdrantUnavailable,
    QdrantVectorStore,
    _point_id,
)
from examlops.vector_store.sparse import tokenize  # noqa: E402

# ── the in-process qdrant-client double ───────────────────────────────────────


class _Obj:
    """A tiny attribute bag standing in for a qdrant-client pydantic model."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.__dict__})"


class _Enum(str):
    pass


class fake_models:  # noqa: N801 - it stands in for the `qdrant_client.models` module
    class Distance:
        COSINE = _Enum("Cosine")
        EUCLID = _Enum("Euclid")
        DOT = _Enum("Dot")

    class TextIndexType:
        TEXT = _Enum("text")

    class PayloadSchemaType:
        FLOAT = _Enum("float")
        KEYWORD = _Enum("keyword")

    class Direction:
        ASC = _Enum("asc")
        DESC = _Enum("desc")

    @staticmethod
    def VectorParams(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(**kw)

    @staticmethod
    def HnswConfigDiff(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(**kw)

    @staticmethod
    def OptimizersConfigDiff(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(**kw)

    @staticmethod
    def SearchParams(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(**kw)

    @staticmethod
    def TextIndexParams(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(**kw)

    @staticmethod
    def PointStruct(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(**kw)

    @staticmethod
    def PointIdsList(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(kind="ids", **kw)

    @staticmethod
    def FilterSelector(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(kind="filter", **kw)

    @staticmethod
    def Filter(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(must=kw.get("must"), should=kw.get("should"))

    @staticmethod
    def FieldCondition(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(key=kw["key"], match=kw.get("match"), range=kw.get("range"))

    @staticmethod
    def MatchValue(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(kind="value", value=kw["value"])

    @staticmethod
    def MatchText(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(kind="text", text=kw["text"])

    @staticmethod
    def Range(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(lt=kw.get("lt"), gt=kw.get("gt"), gte=kw.get("gte"), lte=kw.get("lte"))

    @staticmethod
    def OrderBy(**kw: Any) -> _Obj:  # noqa: N802
        return _Obj(key=kw["key"], direction=kw.get("direction"))


def _nested(payload: dict, key: str) -> Any:
    cur: Any = payload
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class FakeQdrantClient:
    """An in-memory Qdrant faithful to the behaviours this adapter relies on."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    # -- collections ----------------------------------------------------------

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    def create_collection(
        self,
        collection_name: str,
        vectors_config: Any,
        hnsw_config: Any = None,
        optimizers_config: Any = None,
    ) -> None:
        if collection_name in self.collections:
            raise RuntimeError(f"collection {collection_name} already exists")
        self.collections[collection_name] = {
            "size": int(vectors_config.size),
            "distance": str(vectors_config.distance),
            "hnsw": hnsw_config,
            "optimizers": optimizers_config,
            "points": {},
            "indexes": {},
            "indexed_vectors_count": 0,
        }
        self.calls.append(f"create:{collection_name}")

    def delete_collection(self, collection_name: str) -> bool:
        return self.collections.pop(collection_name, None) is not None

    def update_collection(
        self, collection_name: str, hnsw_config: Any = None, optimizers_config: Any = None
    ) -> None:
        coll = self._coll(collection_name)
        coll["hnsw"] = hnsw_config
        coll["optimizers"] = optimizers_config
        self.calls.append(f"update:{collection_name}")

    def get_collection(self, collection_name: str) -> _Obj:
        coll = self._coll(collection_name)
        return _Obj(
            points_count=len(coll["points"]),
            indexed_vectors_count=coll["indexed_vectors_count"],
            status="green",
        )

    def create_payload_index(
        self, collection_name: str, field_name: str, field_schema: Any
    ) -> None:
        self._coll(collection_name)["indexes"][field_name] = field_schema

    def _coll(self, name: str) -> dict[str, Any]:
        if name not in self.collections:
            raise RuntimeError(f"Not found: Collection `{name}` doesn't exist!")
        return self.collections[name]

    # -- points ---------------------------------------------------------------

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        coll = self._coll(collection_name)
        for p in points:
            # Qdrant accepts only an unsigned integer or a UUID as a point id.
            if not isinstance(p.id, int):
                uuid.UUID(str(p.id))
            if len(p.vector) != coll["size"]:
                raise RuntimeError(f"Wrong input: vector dim {len(p.vector)} != {coll['size']}")
            vector = [float(x) for x in p.vector]
            if coll["distance"] == "Cosine":  # Qdrant normalises on write
                norm = math.sqrt(sum(x * x for x in vector))
                vector = [x / norm for x in vector] if norm else vector
            coll["points"][p.id] = {"vector": vector, "payload": dict(p.payload or {})}

    def retrieve(self, collection_name: str, ids: list[Any], with_payload: bool = True) -> list:
        coll = self._coll(collection_name)
        return [
            _Obj(id=i, payload=dict(coll["points"][i]["payload"]))
            for i in ids
            if i in coll["points"]
        ]

    def count(self, collection_name: str, exact: bool = True) -> _Obj:
        return _Obj(count=len(self._coll(collection_name)["points"]))

    def delete(self, collection_name: str, points_selector: Any) -> None:
        coll = self._coll(collection_name)
        if getattr(points_selector, "kind", None) == "ids":
            for i in points_selector.points:
                coll["points"].pop(i, None)
            return
        keep = {
            i: p
            for i, p in coll["points"].items()
            if not self._matches(coll, points_selector.filter, p["payload"])
        }
        coll["points"] = keep

    # -- filtering ------------------------------------------------------------

    def _condition(self, coll: dict[str, Any], cond: Any, payload: dict) -> bool:
        value = _nested(payload, cond.key)
        if cond.match is not None:
            if cond.match.kind == "value":
                return bool(value == cond.match.value)
            # A full-text condition is a server error without a full-text index on the field.
            if coll["indexes"].get(cond.key) is None:
                raise RuntimeError(
                    f'Bad request: Index required but not found for "{cond.key}" of one of '
                    "the following types: [text]"
                )
            return cond.match.text.lower() in tokenize(value if isinstance(value, str) else "")
        rng = cond.range
        if rng is not None:
            if value is None:
                return False
            v = float(value)
            for op, ok in (
                ("lt", lambda a, b: a < b),
                ("lte", lambda a, b: a <= b),
                ("gt", lambda a, b: a > b),
                ("gte", lambda a, b: a >= b),
            ):
                bound = getattr(rng, op, None)
                if bound is not None and not ok(v, float(bound)):
                    return False
            return True
        return True

    def _matches(self, coll: dict[str, Any], flt: Any, payload: dict) -> bool:
        if flt is None:
            return True
        must = flt.must or []
        should = flt.should or []
        if not all(self._condition(coll, c, payload) for c in must):
            return False
        if should and not any(self._condition(coll, c, payload) for c in should):
            return False
        return True

    # -- search ---------------------------------------------------------------

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        limit: int,
        query_filter: Any = None,
        search_params: Any = None,
        with_payload: bool = True,
    ) -> _Obj:
        coll = self._coll(collection_name)
        if len(query) != coll["size"]:
            raise RuntimeError("Wrong input: query vector dim mismatch")
        q = list(query)
        if coll["distance"] == "Cosine":
            norm = math.sqrt(sum(x * x for x in q))
            q = [x / norm for x in q] if norm else q
        scored = []
        for pid, p in coll["points"].items():
            if not self._matches(coll, query_filter, p["payload"]):
                continue
            v = p["vector"]
            if coll["distance"] == "Euclid":
                score = math.sqrt(sum((a - b) ** 2 for a, b in zip(q, v)))
            else:  # Cosine (both sides unit) and Dot are the same arithmetic here
                score = sum(a * b for a, b in zip(q, v))
            scored.append(_Obj(id=pid, score=score, payload=dict(p["payload"])))
        ascending = coll["distance"] == "Euclid"  # Euclid is a distance: nearest first
        scored.sort(key=lambda s: (s.score if ascending else -s.score, str(s.id)))
        return _Obj(points=scored[: int(limit)])

    def scroll(
        self,
        collection_name: str,
        scroll_filter: Any = None,
        limit: int = 10,
        order_by: Any = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> tuple[list, Any]:
        coll = self._coll(collection_name)
        rows = [
            _Obj(
                id=pid,
                payload=dict(p["payload"]),
                vector=list(p["vector"]) if with_vectors else None,
            )
            for pid, p in coll["points"].items()
            if self._matches(coll, scroll_filter, p["payload"])
        ]
        if order_by is not None:
            if coll["indexes"].get(order_by.key) is None:
                raise RuntimeError(f"Bad request: order_by field '{order_by.key}' is not indexed")
            rows.sort(
                key=lambda r: float(_nested(r.payload, order_by.key) or 0.0),
                reverse=str(order_by.direction) == "desc",
            )
        rows = rows[: int(limit)]
        return rows, None


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_QDRANT_INDEXING_THRESHOLD_KB", raising=False)
    monkeypatch.delenv("EXAMLOPS_QDRANT_NAMESPACE", raising=False)
    init_db()


@pytest.fixture
def fake():
    return FakeQdrantClient()


@pytest.fixture
def store(fake):
    return QdrantVectorStore(client=fake, models=fake_models)


@pytest.fixture
def lite():
    return vs.SqliteVectorStore()


ITEMS = [
    vs.VecItem("s1", [1.0, 0.0, 0.0], {"lang": "en"}, "training jobs fail when memory runs out"),
    vs.VecItem("s2", [0.95, 0.1, 0.0], {"lang": "en"}, "a failed training job is retried"),
    vs.VecItem("s3", [0.9, 0.2, 0.05], {"lang": "it"}, "il job di training e fallito"),
    vs.VecItem("s4", [0.5, 0.8, 0.1], {"lang": "en"}, "jobs are scheduled by flux"),
    vs.VecItem("incident", [0.0, 0.1, 1.0], {"lang": "en"}, "JPCP-4711 crashed on gpu01"),
]
Q = [1.0, 0.05, 0.0]


def _load(s, metric="cosine", index=None, tenant="acme"):
    s.create_collection("kb", 3, metric, tenant, index=index)
    s.upsert("kb", list(ITEMS), tenant)


# ── wiring: selection and the missing package ─────────────────────────────────


def test_it_implements_the_vector_store_protocol(store):
    assert isinstance(store, vs.VectorStore)
    assert store.name == "qdrant"


def test_select_store_reaches_qdrant_through_the_one_backend_switch(monkeypatch):
    """No parallel switch: the same env var that picks sqlite/pgvector picks qdrant."""
    assert "qdrant" in vs._STORES
    monkeypatch.setenv("EXAMLOPS_VECTOR_BACKEND", "qdrant")
    monkeypatch.delenv("EXAMLOPS_QDRANT_URL", raising=False)
    with pytest.raises(QdrantUnavailable) as exc:
        vs.select_store()
    assert "EXAMLOPS_QDRANT_URL" in str(exc.value)
    assert "sqlite" in str(exc.value)  # it says what to do instead


def test_a_missing_qdrant_client_is_an_actionable_error_not_a_traceback(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_QDRANT_URL", "http://localhost:6333")
    monkeypatch.setitem(sys.modules, "qdrant_client", None)  # forces ImportError on import
    with pytest.raises(QdrantUnavailable) as exc:
        QdrantVectorStore()
    assert "pip install 'examlops[qdrant]'" in str(exc.value)


def test_an_unknown_backend_is_still_an_error(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VECTOR_BACKEND", "qdrnat")
    with pytest.raises(ValueError, match="qdrant"):
        vs.select_store()


def test_the_namespace_must_be_a_plain_identifier(fake):
    with pytest.raises(ValueError, match="NAMESPACE"):
        QdrantVectorStore(client=fake, models=fake_models, namespace="a; DROP TABLE")


def test_the_namespace_separates_two_instances_on_one_server(fake):
    a = QdrantVectorStore(client=fake, models=fake_models, namespace="one")
    b = QdrantVectorStore(client=fake, models=fake_models, namespace="two")
    a.create_collection("kb", 2, "cosine", "acme")
    a.upsert("kb", [vs.VecItem("x", [1.0, 0.0])], "acme")
    with pytest.raises(vs.CollectionNotFound):
        b.stats("kb", "acme")


# ── the protocol ──────────────────────────────────────────────────────────────


def test_dense_search_ranks_nearest_first(store):
    _load(store)
    assert [h.id for h in store.search("kb", Q, 3, None, "acme")] == ["s1", "s2", "s3"]
    assert store.search("kb", Q, 1, None, "acme")[0].score == pytest.approx(0.9988, abs=1e-3)


def test_l2_scores_are_negated_distances_like_the_other_stores(store, lite):
    for s in (store, lite):
        s.create_collection("c", 2, "l2", "acme")
        s.upsert("c", [vs.VecItem("close", [1.0, 1.0]), vs.VecItem("far", [9.0, 9.0])], "acme")
    got = store.search("c", [1.1, 1.1], 2, None, "acme")
    want = lite.search("c", [1.1, 1.1], 2, None, "acme")
    assert [h.id for h in got] == [h.id for h in want] == ["close", "far"]
    assert got[0].score == pytest.approx(want[0].score, abs=1e-5)
    assert got[0].score < 0  # "higher is better" for every metric, so a distance is negated


def test_dim_and_nan_are_refused_on_upsert_and_query(store):
    _load(store)
    with pytest.raises(vs.DimensionMismatch):
        store.upsert("kb", [vs.VecItem("bad", [1.0, 2.0])], "acme")
    with pytest.raises(vs.DimensionMismatch):
        store.search("kb", [1.0, 2.0], 3, None, "acme")
    with pytest.raises(vs.DimensionMismatch):
        store.hybrid_search("kb", [1.0, 2.0], "x", 3, None, "acme")
    with pytest.raises(ValueError, match="NaN"):
        store.upsert("kb", [vs.VecItem("nan", [float("nan"), 0.0, 0.0])], "acme")


def test_metadata_filter_is_applied_before_ranking(store):
    _load(store)
    assert [h.id for h in store.search("kb", Q, 5, {"lang": "it"}, "acme")] == ["s3"]
    assert store.search("kb", Q, 5, {"lang": "de"}, "acme") == []


def test_a_filter_value_qdrant_cannot_match_is_refused_clearly(store):
    _load(store)
    with pytest.raises(ValueError, match="string, integer and boolean"):
        store.search("kb", Q, 5, {"score": 1.5}, "acme")


def test_tenant_isolation(store):
    _load(store, tenant="tenantA")
    with pytest.raises(vs.CollectionNotFound):
        store.search("kb", Q, 5, None, "tenantB")
    with pytest.raises(vs.CollectionNotFound):
        store.stats("kb", "tenantB")


def test_schema_conflict_and_encoder_stamp(store):
    _load(store)
    with pytest.raises(vs.SchemaConflict):
        store.create_collection("kb", 4, "cosine", "acme")
    store.create_collection("stamped", 3, "cosine", "acme", encoder_id="minilm@v1")
    store.upsert("stamped", [vs.VecItem("x", [1.0, 0.0, 0.0])], "acme", encoder_id="minilm@v1")
    with pytest.raises(vs.EncoderMismatch):
        store.search("stamped", Q, 1, None, "acme", encoder_id="e5@v2")
    store.create_collection("stamped", 3, "cosine", "acme")  # a re-declare keeps the stamp
    assert store.stats("stamped", "acme")["encoder_id"] == "minilm@v1"


def test_an_empty_collection_may_change_shape(store):
    store.create_collection("c", 3, "cosine", "acme")
    store.create_collection("c", 5, "l2", "acme")
    store.upsert("c", [vs.VecItem("a", [1.0, 0.0, 0.0, 0.0, 0.0])], "acme")
    st = store.stats("c", "acme")
    assert st["dim"] == 5 and st["metric"] == "l2" and st["count"] == 1


def test_invalid_metric_and_ivfflat_are_refused(store):
    with pytest.raises(ValueError):
        store.create_collection("c", 2, "manhattan", "acme")
    with pytest.raises(vs.IndexConfigError, match="IVFFlat"):
        store.create_collection("c", 2, "cosine", "acme", index=IndexConfig.build("ivfflat"))
    store.create_collection("c", 2, "cosine", "acme")
    with pytest.raises(vs.IndexConfigError, match="IVFFlat"):
        store.reindex("c", "acme", index=IndexConfig.build("ivfflat"))


def test_upsert_replaces_and_arbitrary_string_ids_survive_the_uuid_mapping(store, fake):
    """Qdrant takes only a UUID or an integer id; the caller's string must come back intact."""
    _load(store)
    store.upsert("kb", [vs.VecItem("s1", [0.0, 0.0, 1.0], {"v": 2}, "moved")], "acme")
    assert store.count("kb", "acme") == 5  # replaced, not duplicated
    hit = store.search("kb", [0.0, 0.0, 1.0], 1, {"v": 2}, "acme")[0]
    assert hit.id == "s1" and hit.metadata == {"v": 2}
    physical = store.stats("kb", "acme")["qdrant_collection"]
    stored = fake.collections[physical]["points"]
    assert _point_id("s1") in stored and "s1" not in stored
    assert stored[_point_id("s1")]["payload"]["eid"] == "s1"


def test_sparse_search_needs_the_full_text_index_and_honours_the_filter(store):
    _load(store)
    assert [h.id for h in store.sparse_search("kb", "JPCP-4711", 3, None, "acme")] == ["incident"]
    assert [h.id for h in store.sparse_search("kb", "training", 5, {"lang": "it"}, "acme")] == [
        "s3"
    ]
    assert store.sparse_search("kb", "   ", 5, None, "acme") == []


def test_hybrid_finds_the_identifier_the_embedding_misses_and_reports_the_channels(store):
    _load(store)
    hits = store.hybrid_search("kb", Q, "why did JPCP-4711 fail", 3, None, "acme")
    assert "incident" in [h.id for h in hits]
    assert "incident" not in [h.id for h in store.search("kb", Q, 3, None, "acme")]
    inc = next(h for h in hits if h.id == "incident")
    assert inc.channels["sparse_rank"] == 1.0
    with pytest.raises(ValueError, match="fusion"):
        store.hybrid_search("kb", Q, "x", 3, None, "acme", fusion="magic")


def test_hybrid_with_no_text_degrades_to_the_dense_ranking(store):
    _load(store)
    fused = [h.id for h in store.hybrid_search("kb", Q, "", 3, None, "acme")]
    assert fused == [h.id for h in store.search("kb", Q, 3, None, "acme")]


def test_trim_keeps_the_newest_and_scan_returns_them_newest_first(store):
    store.create_collection("ring", 2, "l2", "acme")
    for i in range(6):
        store.upsert("ring", [vs.VecItem(f"i{i}", [float(i), 0.0], {"n": i})], "acme")
    assert store.trim("ring", "acme", 10) == 0  # nothing to evict
    assert store.trim("ring", "acme", 3) == 3
    assert [it.id for it in store.scan("ring", "acme", 10)] == ["i5", "i4", "i3"]
    assert store.scan("ring", "acme", 0) == []
    items = store.scan("ring", "acme", 1)
    assert items[0].metadata == {"n": 5} and items[0].vector == [5.0, 0.0]
    assert store.trim("ring", "acme", 0) == 3 and store.count("ring", "acme") == 0


def test_drop_removes_the_collection_and_frees_the_name(store):
    _load(store)
    assert store.drop_collection("kb", "acme") == 5
    with pytest.raises(vs.CollectionNotFound):
        store.stats("kb", "acme")
    _load(store)
    assert store.count("kb", "acme") == 5


def test_stats_says_what_actually_answers_a_query(store, fake):
    _load(store, index=IndexConfig.build("hnsw", m=8, ef_construction=32))
    st = store.stats("kb", "acme")
    assert st["backend"] == "qdrant" and st["count"] == 5
    assert st["index"] == {"type": "hnsw", "m": 8, "ef_construction": 32, "ef_search": 40}
    # Declared is not built: Qdrant leaves a small segment unindexed and scans it.
    assert st["search"].startswith("exact (HNSW declared but not built")
    assert st["indexed_vectors"] == 0
    fake.collections[st["qdrant_collection"]]["indexed_vectors_count"] = 5
    assert store.stats("kb", "acme")["search"] == "ann"
    store.reindex("kb", "acme", index=IndexConfig.build("flat"))
    assert store.stats("kb", "acme")["search"] == "exact"


def test_reindex_retunes_the_graph_and_keeps_recall(store, fake):
    _load(store, index=IndexConfig.build("hnsw", m=8, ef_construction=32))
    before = [h.id for h in store.search("kb", Q, 5, None, "acme")]
    store.reindex("kb", "acme", index=IndexConfig.build("hnsw", m=32, ef_construction=80))
    assert store.stats("kb", "acme")["index"]["m"] == 32
    assert [h.id for h in store.search("kb", Q, 5, None, "acme")] == before
    physical = store.stats("kb", "acme")["qdrant_collection"]
    assert fake.collections[physical]["hnsw"].m == 32


def test_the_indexing_threshold_override_reaches_qdrant(fake, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_QDRANT_INDEXING_THRESHOLD_KB", "1")
    s = QdrantVectorStore(client=fake, models=fake_models)
    s.create_collection("kb", 3, "cosine", "acme", index=IndexConfig.build("hnsw"))
    physical = s.stats("kb", "acme")["qdrant_collection"]
    assert fake.collections[physical]["optimizers"].indexing_threshold == 1


def test_every_operation_is_recorded_to_the_shared_metrics_table(store):
    _load(store)
    store.search("kb", Q, 2, None, "acme")
    store.sparse_search("kb", "training", 2, None, "acme")
    store.hybrid_search("kb", Q, "training", 2, None, "acme")
    store.trim("kb", "acme", 2)
    store.drop_collection("kb", "acme")
    from examlops.platform_db import get_db

    with get_db() as conn:
        ops = {r["operation"] for r in conn.execute("SELECT operation FROM vector_metrics")}
    assert {"upsert", "search", "sparse_search", "hybrid_search", "trim", "drop"} <= ops


def test_ranking_matches_the_sqlite_fallback_on_the_double(store, lite):
    """The parity claim, on the double. The live suite repeats it against a real server."""
    for metric in ("cosine", "l2", "dot"):
        name = f"kb_{metric}"
        for s in (store, lite):
            s.create_collection(name, 3, metric, "acme")
            s.upsert(name, list(ITEMS), "acme")
        for flt in (None, {"lang": "en"}):
            got = store.search(name, Q, 5, flt, "acme")
            want = lite.search(name, Q, 5, flt, "acme")
            assert [h.id for h in got] == [h.id for h in want], (metric, flt)
            for g, w in zip(got, want):
                assert g.score == pytest.approx(w.score, abs=1e-5), (metric, flt)


# ── live suite ────────────────────────────────────────────────────────────────

URL = os.getenv("EXAMLOPS_QDRANT_TEST_URL")


@pytest.fixture
def real(_env):
    s = QdrantVectorStore(URL, namespace=f"t{uuid.uuid4().hex[:10]}")
    yield s
    for name in list(s._client.get_collections().collections):
        if name.name.startswith(s.namespace):
            s._client.delete_collection(collection_name=name.name)


@pytest.mark.parametrize("metric", ["cosine", "l2", "dot"])
@pytest.mark.parametrize("flt", [None, {"lang": "en"}])
@pytest.mark.live
@pytest.mark.skipif(not URL, reason="EXAMLOPS_QDRANT_TEST_URL not set (see: make qdrant-live)")
def test_live_dense_ranking_matches_the_sqlite_fallback(real, lite, metric, flt):
    _load(real, metric)
    _load(lite, metric)
    got = real.search("kb", Q, 5, flt, "acme")
    want = lite.search("kb", Q, 5, flt, "acme")
    assert [h.id for h in got] == [h.id for h in want]
    for g, w in zip(got, want):
        assert g.score == pytest.approx(w.score, abs=1e-5)  # Qdrant stores float32


@pytest.mark.live
@pytest.mark.skipif(not URL, reason="EXAMLOPS_QDRANT_TEST_URL not set (see: make qdrant-live)")
def test_live_hybrid_and_sparse_find_the_identifier(real, lite):
    _load(real, "cosine")
    _load(lite, "cosine")
    text = "why did JPCP-4711 fail"
    got = real.hybrid_search("kb", Q, text, 3, None, "acme")
    assert "incident" in [h.id for h in got]
    assert "incident" not in [h.id for h in real.search("kb", Q, 3, None, "acme")]
    assert {h.id for h in got} == {h.id for h in lite.hybrid_search("kb", Q, text, 3, None, "acme")}
    assert next(h for h in got if h.id == "incident").channels["sparse_rank"] == 1.0
    assert [h.id for h in real.sparse_search("kb", "JPCP-4711", 3, None, "acme")] == ["incident"]
    assert [h.id for h in real.sparse_search("kb", "training", 5, {"lang": "it"}, "acme")] == ["s3"]
    assert real.sparse_search("kb", "   ", 5, None, "acme") == []


@pytest.mark.live
@pytest.mark.skipif(not URL, reason="EXAMLOPS_QDRANT_TEST_URL not set (see: make qdrant-live)")
def test_live_contract_guards_match_the_other_stores(real):
    _load(real, "cosine")
    with pytest.raises(vs.DimensionMismatch):
        real.upsert("kb", [vs.VecItem("bad", [1.0, 2.0])], "acme")
    with pytest.raises(vs.DimensionMismatch):
        real.search("kb", [1.0, 2.0], 3, None, "acme")
    with pytest.raises(ValueError, match="NaN"):
        real.upsert("kb", [vs.VecItem("nan", [float("nan"), 0.0, 0.0])], "acme")
    with pytest.raises(vs.SchemaConflict):
        real.create_collection("kb", 4, "cosine", "acme")
    with pytest.raises(vs.CollectionNotFound):
        real.search("kb", Q, 3, None, "other-tenant")
    real.create_collection("stamped", 3, "cosine", "acme", encoder_id="minilm@v1")
    real.upsert("stamped", [vs.VecItem("x", [1.0, 0.0, 0.0])], "acme", encoder_id="minilm@v1")
    with pytest.raises(vs.EncoderMismatch):
        real.search("stamped", Q, 1, None, "acme", encoder_id="e5@v2")
    real.create_collection("stamped", 3, "cosine", "acme")
    assert real.stats("stamped", "acme")["encoder_id"] == "minilm@v1"


@pytest.mark.live
@pytest.mark.skipif(not URL, reason="EXAMLOPS_QDRANT_TEST_URL not set (see: make qdrant-live)")
def test_live_ring_buffer_scan_reindex_and_drop(real):
    real.create_collection("ring", 2, "l2", "acme", index=IndexConfig.build("hnsw", m=8))
    for i in range(6):
        real.upsert("ring", [vs.VecItem(f"i{i}", [float(i), 0.0], {"n": i})], "acme")
    assert real.trim("ring", "acme", 3) == 3
    assert [it.id for it in real.scan("ring", "acme", 10)] == ["i5", "i4", "i3"]
    before = [h.id for h in real.search("ring", [5.0, 0.0], 3, None, "acme")]
    real.reindex("ring", "acme", index=IndexConfig.build("flat"))
    assert real.stats("ring", "acme")["search"] == "exact"
    assert [h.id for h in real.search("ring", [5.0, 0.0], 3, None, "acme")] == before
    assert real.drop_collection("ring", "acme") == 3
    with pytest.raises(vs.CollectionNotFound):
        real.stats("ring", "acme")
