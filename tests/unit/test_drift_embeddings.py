"""ADR 0020 clause 4 — sampled per-inference embeddings reach the vector store as drift samples.

Covers the module (sampling, content-addressed ids, ring buffer, baseline, nearest-neighbour
drift), the seam's new ``trim``/``scan``, the consumer path, and the CLI read path. The bridge
side (worker-thread write, counted failure, event payload) is in test_dataplane_bus_bridge.py.
"""

from __future__ import annotations

import json

import pytest

from examlops import drift_embeddings as de
from examlops.vector_store import SqliteVectorStore, VecItem


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in (de.RATE_ENV, de.CAP_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(de, "_stats", {"stored": 0, "evicted": 0, "failed": 0})


def _v(i: int, dim: int = 4) -> list[float]:
    return [1.0 + i * 0.001] + [float(i % 3)] * (dim - 1)


# ── sampling ────────────────────────────────────────────────────────────────


def test_sampling_is_off_by_default_and_env_tunable(monkeypatch):
    assert de.sample_rate() == 0.0
    assert not any(de.should_sample("M", f"j{i}") for i in range(200))
    assert de.maybe_sample("M", "Production", "3", "j1", [1.0, 2.0]) is None
    monkeypatch.setenv(de.RATE_ENV, "1")
    assert de.maybe_sample("M", "Production", "3", "j1", [1.0, 2.0])["version"] == "3"
    monkeypatch.setenv(de.RATE_ENV, "garbage")
    assert de.sample_rate() == 0.0  # fail closed


def test_sample_rate_is_roughly_honoured_and_deterministic(monkeypatch):
    monkeypatch.setenv(de.RATE_ENV, "0.25")
    picks = [de.should_sample("M", f"job-{i}") for i in range(4000)]
    assert 0.20 < sum(picks) / len(picks) < 0.30
    assert picks == [de.should_sample("M", f"job-{i}") for i in range(4000)]


def test_cap_is_clamped(monkeypatch):
    assert de.ring_cap() == de.DEFAULT_CAP
    monkeypatch.setenv(de.CAP_ENV, "0")
    assert de.ring_cap() == 1
    monkeypatch.setenv(de.CAP_ENV, "10**9")
    assert de.ring_cap() == de.DEFAULT_CAP  # unparseable


# ── ingest ──────────────────────────────────────────────────────────────────


def test_record_sample_stores_model_version_ts_and_content_addressed_id():
    sid = de.record_sample("M", "Production", "7", "job-1", [1.0, 2.0, 3.0], ts=1000.0)
    assert sid == de.sample_id("M", "Production", [1.0, 2.0, 3.0])
    assert sid.startswith("emb-")
    (item,) = SqliteVectorStore().scan(de.samples_collection("M"), "default", 10)
    assert item.id == sid and item.vector == [1.0, 2.0, 3.0]
    assert item.metadata == {
        "model": "M",
        "alias": "Production",
        "version": "7",
        "job_id": "job-1",
        "ts": 1000.0,
    }
    # same content again -> same row, not a second one
    de.record_sample("M", "Production", "7", "job-2", [1.0, 2.0, 3.0])
    assert SqliteVectorStore().count(de.samples_collection("M")) == 1
    # a different alias is a different row
    assert de.sample_id("M", "Canary", [1.0, 2.0, 3.0]) != sid


def test_ring_buffer_keeps_only_the_newest_cap_vectors(monkeypatch):
    monkeypatch.setenv(de.CAP_ENV, "5")
    for i in range(12):
        de.record_sample("M", "P", "1", f"j{i}", _v(i), ts=float(i))
    st = SqliteVectorStore()
    assert st.count(de.samples_collection("M")) == 5
    jobs = {it.metadata["job_id"] for it in st.scan(de.samples_collection("M"), "default", 99)}
    assert jobs == {f"j{i}" for i in range(7, 12)}
    assert de.stats() == {"stored": 12, "evicted": 7, "failed": 0}


def test_ring_is_per_model(monkeypatch):
    monkeypatch.setenv(de.CAP_ENV, "2")
    for i in range(4):
        de.record_sample("A", "P", "1", f"a{i}", _v(i))
        de.record_sample("B", "P", "1", f"b{i}", _v(i))
    st = SqliteVectorStore()
    assert st.count(de.samples_collection("A")) == st.count(de.samples_collection("B")) == 2


@pytest.mark.parametrize("bad", [[], [1.0, float("nan")], [float("inf"), 1.0]])
def test_bad_vectors_are_refused_not_stored(bad):
    with pytest.raises(ValueError):
        de.record_sample("M", "P", "1", "j", bad)
    assert de.stats()["stored"] == 0


def test_dimension_change_raises_for_the_caller_to_count():
    de.record_sample("M", "P", "1", "j1", [1.0, 2.0])
    with pytest.raises(ValueError):
        de.record_sample("M", "P", "1", "j2", [1.0, 2.0, 3.0])


# ── seam: trim / scan ───────────────────────────────────────────────────────


def test_seam_trim_and_scan():
    st = SqliteVectorStore()
    st.create_collection("c", 2, "cosine", "default")
    st.upsert("c", [VecItem(f"i{n}", [1.0, float(n)]) for n in range(6)], "default")
    assert [i.id for i in st.scan("c", "default", 2)] == ["i5", "i4"]  # newest first
    assert st.trim("c", "default", 4) == 2
    assert st.trim("c", "default", 4) == 0
    assert st.count("c") == 4
    assert st.trim("c", "default", 0) == 4


# ── baseline + nearest-neighbour read path ──────────────────────────────────


def test_drift_reports_missing_baseline_and_samples():
    assert de.embedding_drift("M")["status"] == "no_baseline"
    with pytest.raises(ValueError):
        de.set_baseline("M")
    de.record_sample("M", "P", "1", "j0", [1.0, 0.0])
    de.set_baseline("M")
    SqliteVectorStore().drop_collection(de.samples_collection("M"))
    assert de.embedding_drift("M")["status"] == "no_samples"


def test_nearest_neighbour_drift_detects_a_moved_input_distribution():
    for i in range(20):  # baseline era: vectors all near the x axis
        de.record_sample("M", "P", "1", f"old{i}", [1.0, 0.01 * i], ts=float(i))
    assert de.set_baseline("M")["n"] == 20
    ok = de.embedding_drift("M", k=3)
    assert ok["status"] == "ok" and ok["drifted"] is False
    assert ok["recent_mean_similarity"] > 0.99 and len(ok["nearest"]) == 3
    for i in range(30):  # input distribution moves to the y axis
        de.record_sample("M", "P", "2", f"new{i}", [0.05 * i, 1.0], ts=100.0 + i)
    res = de.embedding_drift("M", k=3, min_similarity=0.8)
    assert res["drifted"] is True and res["recent_mean_similarity"] < 0.8
    assert res["farthest"][0]["similarity"] <= res["farthest"][-1]["similarity"]
    assert res["farthest"][0]["version"] == "2" and res["nearest"][0]["version"] == "1"
    assert res["min_sample_similarity"] <= res["mean_similarity"]


# ── consumer path (serving.inference_telemetry) ─────────────────────────────


def _event(**extra):
    from examlops.platform_db import init_db

    init_db()
    d = {"model": "M", "alias": "P", "job_id": "j1", "prediction": 1.0}
    d.update(extra)
    return {"data": d}


def test_consumer_stores_the_sample_carried_by_the_event():
    from examlops.data.drift import handle_inference_telemetry_event

    handle_inference_telemetry_event(
        _event(embedding_sample={"vector": [1.0, 2.0], "version": "4", "ts": 5.0})
    )
    (item,) = SqliteVectorStore().scan(de.samples_collection("M"), "default", 5)
    assert item.metadata["version"] == "4" and item.metadata["ts"] == 5.0


def test_consumer_sample_failure_is_counted_and_does_not_undo_the_snapshots():
    from examlops.data.drift import handle_inference_telemetry_event
    from examlops.platform_db import get_db

    handle_inference_telemetry_event(_event(embedding_sample={"vector": [], "ts": 1.0}))
    assert de.stats()["failed"] == 1  # counted, not raised
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM drift_snapshots").fetchone()["c"]
    assert n == 1


def test_event_schema_accepts_the_sample():
    from examlops.events.schemas import schema_for

    props = schema_for("serving.inference_telemetry")["properties"]
    assert "embedding_sample" in props


# ── CLI read path: `exa drift input status --similarity` / `input baseline` ──


def _cli(*args):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    return CliRunner().invoke(app, ["--json", *args])


def test_cli_similarity_reports_json_and_status():
    r = _cli("drift", "input", "status", "M", "--similarity")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["status"] == "no_baseline"
    for i in range(5):
        de.record_sample("M", "P", "1", f"j{i}", [1.0, 0.1 * i])
    de.set_baseline("M")
    out = json.loads(_cli("drift", "input", "status", "M", "--similarity", "-k", "2").stdout)
    assert out["status"] == "ok" and out["samples"] == 5 and len(out["nearest"]) == 2


def test_cli_similarity_needs_a_model():
    assert _cli("drift", "input", "status", "--similarity").exit_code != 0
