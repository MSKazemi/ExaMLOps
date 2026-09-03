"""ADR 0043 clauses 1 and 2 — vectors carry the encoder that made them, and the guard guards.

The recorded gap was blunt: "the guard has nothing to guard". `guard_compatible` existed and
refused cross-encoder comparisons, and **no vector, collection or cache entry in the tree carried
an encoder_id**, so it was reached by nothing and the silent-corruption failure the ADR exists to
prevent was still open.

That failure is specifically *not* the dimension check. A wrong dimension cannot be scored at all.
Two encoders of the **same** dimension produce vectors that score against each other perfectly
happily and mean nothing — the search returns confident, ranked, wrong results and nothing
downstream can tell.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.platform_db import init_db  # noqa: E402
from examlops.vector_store import (  # noqa: E402
    DimensionMismatch,
    EncoderMismatch,
    SqliteVectorStore,
    VecItem,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)
    init_db()


@pytest.fixture
def store():
    return SqliteVectorStore()


V = [1.0, 0.0, 0.0]


# ── clause 1: the stamp exists ────────────────────────────────────────────────


def test_a_collection_records_the_encoder_that_built_it(store):
    store.create_collection("c", 3, encoder_id="minilm@v1")
    assert store._collection("c", "default")["encoder_id"] == "minilm@v1"


def test_a_collection_can_still_be_created_without_one(store):
    """Additive: every existing caller keeps working, and the column is NULL for them."""
    store.create_collection("c", 3)
    assert store._collection("c", "default")["encoder_id"] is None


# ── clause 2: the guard refuses ───────────────────────────────────────────────


def test_searching_with_a_different_encoder_is_refused(store):
    store.create_collection("c", 3, encoder_id="minilm@v1")
    store.upsert("c", [VecItem("1", V, {})], encoder_id="minilm@v1")
    with pytest.raises(EncoderMismatch, match="reindex"):
        store.search("c", V, encoder_id="e5@v2")


def test_upserting_with_a_different_encoder_is_refused(store):
    """Catching it on write matters more than on read — a mixed collection stays wrong until
    someone reindexes it, and every later query is quietly affected."""
    store.create_collection("c", 3, encoder_id="minilm@v1")
    with pytest.raises(EncoderMismatch):
        store.upsert("c", [VecItem("1", V, {})], encoder_id="e5@v2")


def test_the_same_encoder_passes(store):
    store.create_collection("c", 3, encoder_id="minilm@v1")
    store.upsert("c", [VecItem("1", V, {})], encoder_id="minilm@v1")
    assert len(store.search("c", V, encoder_id="minilm@v1")) == 1


def test_this_is_the_failure_the_dimension_check_cannot_see(store):
    """The whole point: identical dims, different encoders. The dim guard is happy; the result
    is meaningless."""
    store.create_collection("c", 3, encoder_id="minilm@v1")
    store.upsert("c", [VecItem("1", V, {})], encoder_id="minilm@v1")
    store.search("c", V, encoder_id="minilm@v1")  # dim 3 is fine either way
    with pytest.raises(EncoderMismatch):
        store.search("c", V, encoder_id="e5@v2")  # same dim 3, still refused


def test_a_wrong_dimension_is_still_its_own_error(store):
    """The two failures stay distinguishable — one is unscoreable, the other is scoreable and
    wrong, and an operator needs to know which."""
    store.create_collection("c", 3, encoder_id="minilm@v1")
    with pytest.raises(DimensionMismatch):
        store.search("c", [1.0, 0.0], encoder_id="minilm@v1")


# ── unverified is not verified ────────────────────────────────────────────────


def test_an_unstamped_collection_cannot_be_checked_and_says_nothing(store):
    """A collection written before the column existed has no encoder to compare against.
    Refusing would break every legacy corpus; claiming compatibility would be a lie. It passes,
    and that is the absence of a check rather than the result of one."""
    store.create_collection("c", 3)
    store.upsert("c", [VecItem("1", V, {})], encoder_id="minilm@v1")
    assert len(store.search("c", V, encoder_id="e5@v2")) == 1


def test_a_caller_that_names_no_encoder_is_not_asserting_compatibility(store):
    """Same distinction as `measured` on an SLO and `usage_reported_rate` on an eval run: you
    cannot mismatch an identity you never asserted."""
    store.create_collection("c", 3, encoder_id="minilm@v1")
    store.upsert("c", [VecItem("1", V, {})])
    assert len(store.search("c", V)) == 1


# ── the guard reaches the function that was written for it ────────────────────


def test_the_check_routes_through_guard_compatible():
    """`guard_compatible` was written for exactly this and reached by nothing — the ADR's own
    finding. Two copies of one rule drift."""
    import inspect

    src = inspect.getsource(SqliteVectorStore._check_encoder)
    assert "guard_compatible" in src


def test_the_two_stores_take_the_same_arguments():
    """A seam whose implementations take different arguments is not a seam: the day pgvector is
    implemented, a caller that stamps its encoder would silently stop being checked."""
    import inspect

    from examlops.vector_store import PgVectorStore

    for method in ("create_collection", "upsert", "search"):
        sqlite_args = set(inspect.signature(getattr(SqliteVectorStore, method)).parameters)
        pg_args = set(inspect.signature(getattr(PgVectorStore, method)).parameters)
        assert "encoder_id" in sqlite_args and "encoder_id" in pg_args, method
