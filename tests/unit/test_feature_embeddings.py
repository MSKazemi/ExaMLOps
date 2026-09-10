# tests/unit/test_feature_embeddings.py
"""ADR 0020 clause 4 (A3 half) — materialized embedding features reach the vector store.

A feature view names one of its features as its embedding; materializing the view indexes each
entity's online embedding into ``features.<view>``, and ``similar_entities`` answers "entities like
this one" from exactly the values serving reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_VECTOR_BACKEND", raising=False)
    from examlops.platform_db import init_db

    init_db()


def _view(embedding: str | None = "embedding"):
    from examlops import feature_store as fs

    fs.apply_view(
        fs.FeatureView("jobs", "job", ["embedding", "pclass"], embedding_feature=embedding)
    )
    return fs


def _ingest(fs, entity: str, ts: str, embedding) -> None:
    values = {"pclass": "compute-bound"}
    if embedding is not None:
        values["embedding"] = embedding
    fs.ingest("jobs", entity, ts, values)


def test_the_embedding_feature_must_be_one_of_the_views_features():
    from examlops import feature_store as fs

    with pytest.raises(ValueError, match="not one of the view's features"):
        fs.apply_view(fs.FeatureView("v", "e", ["a", "b"], embedding_feature="c"))
    fs.apply_view(fs.FeatureView("v", "e", ["a", "b"], embedding_feature="a"))
    assert fs.get_view("v").embedding_feature == "a"
    assert [v.embedding_feature for v in fs.list_views()] == ["a"]


def test_materialize_indexes_valid_embeddings_and_counts_the_rest():
    fs = _view()
    _ingest(fs, "a", "2026-09-10 10:00:00", [1.0, 0.1, 0.0])
    _ingest(fs, "b", "2026-09-10 10:00:00", [1.0, 0.2, 0.0])
    _ingest(fs, "missing", "2026-09-10 10:00:00", None)
    _ingest(fs, "text", "2026-09-10 10:00:00", "not-a-vector")
    _ingest(fs, "nan", "2026-09-10 10:00:00", [float("nan"), 0.0, 0.0])
    _ingest(fs, "short", "2026-09-10 10:00:00", [1.0, 0.0])  # another dimension
    out = fs.materialize_with_index("jobs")
    idx = out["embeddings"]
    assert out["rows"] == 6
    assert (idx.indexed, idx.skipped, idx.dim, idx.error) == (2, 4, 3, None)
    assert any("dim 2 != collection dim 3" in r for r in idx.skipped_reasons)
    from examlops.vector_store import select_store

    assert select_store().count("features.jobs", "default") == 2


def test_similar_entities_excludes_itself_and_ranks_by_cosine():
    fs = _view()
    _ingest(fs, "a", "2026-09-10 10:00:00", [1.0, 0.0, 0.0])
    _ingest(fs, "near", "2026-09-10 10:00:00", [0.9, 0.1, 0.0])
    _ingest(fs, "far", "2026-09-10 10:00:00", [0.0, 0.0, 1.0])
    fs.materialize("jobs")
    hits = fs.similar_entities("jobs", "a", k=2)
    assert [h.id for h in hits] == ["near", "far"]
    assert hits[0].score > hits[1].score


def test_rematerializing_updates_an_entitys_vector():
    fs = _view()
    _ingest(fs, "a", "2026-09-10 10:00:00", [1.0, 0.0, 0.0])
    _ingest(fs, "b", "2026-09-10 10:00:00", [0.0, 1.0, 0.0])
    fs.materialize("jobs")
    _ingest(fs, "b", "2026-09-10 11:00:00", [1.0, 0.0, 0.0])  # b now looks like a
    fs.materialize("jobs")
    assert fs.similar_entities("jobs", "a", k=1)[0].score == pytest.approx(1.0)


def test_the_index_follows_the_online_store_not_newer_offline_rows():
    fs = _view()
    _ingest(fs, "a", "2026-09-10 10:00:00", [1.0, 0.0, 0.0])
    _ingest(fs, "b", "2026-09-10 10:00:00", [0.0, 1.0, 0.0])
    fs.materialize("jobs", end_ts="2026-09-10 10:30:00")
    _ingest(fs, "b", "2026-09-10 11:00:00", [1.0, 0.0, 0.0])  # not materialized yet
    fs.index_embeddings("jobs")  # re-index: still the online (old) value
    assert fs.similar_entities("jobs", "a", k=1)[0].score == pytest.approx(0.0)


def test_an_indexing_failure_never_undoes_a_materialization(monkeypatch):
    fs = _view()
    _ingest(fs, "a", "2026-09-10 10:00:00", [1.0, 0.0, 0.0])
    from examlops import vector_store

    def _down():
        raise RuntimeError("pgvector unreachable")

    monkeypatch.setattr(vector_store, "select_store", lambda name=None: _down())
    out = fs.materialize_with_index("jobs")
    assert out["rows"] == 1
    assert "pgvector unreachable" in out["embeddings"].error
    assert fs.get_online_features("jobs", ["a"])[0]["pclass"] == "compute-bound"


def test_a_view_without_an_embedding_materializes_exactly_as_before():
    fs = _view(embedding=None)
    _ingest(fs, "a", "2026-09-10 10:00:00", [1.0, 0.0, 0.0])
    assert fs.materialize_with_index("jobs") == {"rows": 1, "embeddings": None}
    assert fs.materialize("jobs") == 1
    with pytest.raises(ValueError, match="declares no embedding"):
        fs.similar_entities("jobs", "a")


def test_similar_needs_a_materialized_embedding_for_the_entity():
    fs = _view()
    _ingest(fs, "a", "2026-09-10 10:00:00", [1.0, 0.0, 0.0])
    fs.materialize("jobs")
    with pytest.raises(KeyError, match="no materialized embedding"):
        fs.similar_entities("jobs", "ghost")


def test_cli_apply_materialize_similar():
    from typer.testing import CliRunner

    from examlops.cli import _output
    from examlops.cli.commands.feature_cmd import app

    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            [
                "apply",
                "jobs",
                "--entity",
                "job",
                "--features",
                "embedding,pclass",
                "--embedding",
                "embedding",
            ],
        ).exit_code
        == 0
    )
    for ent, vec in (("a", [1, 0, 0]), ("b", [0.8, 0.2, 0]), ("c", [0, 0, 1])):
        runner.invoke(
            app,
            [
                "ingest",
                "jobs",
                "--entity-id",
                ent,
                "--event-ts",
                "2026-09-10 10:00:00",
                "--values",
                json.dumps({"embedding": vec}),
            ],
        )
    _output.json_mode = True
    try:
        mat = json.loads(runner.invoke(app, ["materialize", "jobs"]).output)
        assert mat["rows"] == 3 and mat["embeddings"]["indexed"] == 3
        sim = json.loads(
            runner.invoke(app, ["similar", "jobs", "--entity-id", "a", "-k", "1"]).output
        )
        assert [s["entity_id"] for s in sim] == ["b"]
    finally:
        _output.json_mode = False
    bad = runner.invoke(app, ["apply", "v", "--entity", "e", "--features", "a", "--embedding", "z"])
    assert bad.exit_code != 0
