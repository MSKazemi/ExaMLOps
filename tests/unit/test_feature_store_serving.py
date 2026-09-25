"""ADR 0017 clause 2, serving half — FeatureTransformer applies the pack's feature view."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
sys.path.insert(0, str(ROOT))

VIEW = """\
name: {name}
entity: job
entity_key: job_id
serving: true
features:
  - {{name: embedding, dtype: vector, dim: {dim}}}
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SERVING_FEATURE_TTL", "0")
    for var in (
        "EXAMLOPS_SERVING_FEATURE_VIEW",
        "EXAMLOPS_SERVING_ONLINE_FEATURES",
        "EXAMLOPS_FEATURE_ONLINE_STORE",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops.feature_store.online import reset_online_store
    from examlops.feature_store.serving import reset_cache
    from examlops.platform_db import init_db

    init_db()
    reset_cache()
    reset_online_store()
    yield
    reset_cache()


def _features_dir(tmp_path, monkeypatch, *, dim=3, name="jobs") -> Path:
    d = tmp_path / "features"
    d.mkdir(exist_ok=True)
    (d / f"{name}.yaml").write_text(VIEW.format(name=name, dim=dim))
    monkeypatch.setenv("EXAMLOPS_FEATURES_DIR", str(d))
    return d


def _transform(req):
    from serving.inference_pipeline.app import FeatureTransformer

    return FeatureTransformer._transform_one(req)


def test_the_definition_file_decides_what_serving_accepts(tmp_path, monkeypatch):
    d = _features_dir(tmp_path, monkeypatch, dim=3)
    out = _transform({"embedding": [1, 2, 3], "num_nodes": 2})
    assert out["features"] == {"embedding": [1.0, 2.0, 3.0]}
    with pytest.raises(ValueError, match="expected 3 dims, got 384"):
        _transform({"embedding": [0.0] * 384, "num_nodes": 2})
    # Change the one definition: serving follows, with no code change.
    (d / "jobs.yaml").write_text(VIEW.format(name="jobs", dim=4))
    assert _transform({"embedding": [1, 2, 3, 4], "num_nodes": 1})["features"]["embedding"] == [
        1.0,
        2.0,
        3.0,
        4.0,
    ]


def test_the_reference_pack_view_is_what_serving_uses_by_default(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(ROOT / "usecases" / "reference"))
    monkeypatch.delenv("EXAMLOPS_FEATURES_DIR", raising=False)
    from examlops.feature_store.serving import resolution

    res = resolution()
    assert res.view is not None and res.view.name == "fdata_job_features"
    assert _transform({"embedding": [0.5] * 384, "num_nodes": 1})["features"] == {
        "embedding": [0.5] * 384
    }


def test_a_request_naming_its_entity_is_served_the_materialized_value(tmp_path, monkeypatch):
    from examlops import feature_store as fs

    _features_dir(tmp_path, monkeypatch)
    fs.apply_view(fs.FeatureView("jobs", "job", ["embedding"]))
    fs.ingest("jobs", "job-42", "2026-09-01 10:00:00", {"embedding": [0.1, 0.2, 0.3]})
    fs.materialize("jobs")
    out = _transform({"job_id": "job-42", "num_nodes": 1})
    assert out["features"] == {"embedding": [0.1, 0.2, 0.3]}
    assert out["job_id"] == "job-42"
    # The request's own features still win over the stored ones.
    own = _transform({"job_id": "job-42", "num_nodes": 1, "embedding": [9, 9, 9]})
    assert own["features"] == {"embedding": [9.0, 9.0, 9.0]}


def test_an_unknown_entity_is_a_validation_error_that_says_so(tmp_path, monkeypatch):
    _features_dir(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="no materialized online features for job_id=ghost"):
        _transform({"job_id": "ghost", "num_nodes": 1})


def test_online_lookup_can_be_switched_off(tmp_path, monkeypatch):
    from examlops import feature_store as fs

    _features_dir(tmp_path, monkeypatch)
    fs.apply_view(fs.FeatureView("jobs", "job", ["embedding"]))
    fs.ingest("jobs", "job-42", "2026-09-01 10:00:00", {"embedding": [0.1, 0.2, 0.3]})
    fs.materialize("jobs")
    monkeypatch.setenv("EXAMLOPS_SERVING_ONLINE_FEATURES", "0")
    with pytest.raises(ValueError, match="embedding is required"):
        _transform({"job_id": "job-42", "num_nodes": 1})


def test_no_resolvable_definition_degrades_to_the_legacy_transform(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_FEATURES_DIR", str(tmp_path / "empty"))
    from examlops.feature_store.serving import resolution

    assert resolution().view is None
    assert _transform({"embedding": [0.0] * 384, "num_nodes": 1})["features"] == {
        "embedding": [0.0] * 384
    }
    with pytest.raises(ValueError, match="expected 384"):
        _transform({"embedding": [0.0] * 3, "num_nodes": 1})


def test_a_broken_definition_degrades_rather_than_taking_inference_down(tmp_path, monkeypatch):
    d = _features_dir(tmp_path, monkeypatch)
    (d / "broken.yaml").write_text("name: x\n")
    from examlops.feature_store.serving import resolution

    res = resolution()
    assert res.view is None and "invalid" in res.reason
    assert _transform({"embedding": [0.0] * 384, "num_nodes": 1})["features"]["embedding"]


def test_the_transform_runs_off_the_event_loop(monkeypatch):
    """A request that names its entity triggers an online-store read (SQLite, or Redis with a
    0.5 s socket timeout). Run on the event loop, a batch of 32 such reads during a Redis outage
    would freeze every other request on the replica for seconds."""
    import asyncio
    import threading

    from serving.inference_pipeline.app import FeatureTransformer

    seen: list[int] = []

    def record(req):
        seen.append(threading.get_ident())
        raise ValueError("stop here")

    monkeypatch.setattr(FeatureTransformer, "_transform_one", staticmethod(record))

    async def run():
        loop_thread = threading.get_ident()
        out = await FeatureTransformer.handle_batch.__wrapped__(
            FeatureTransformer(router=None), [{"job_id": "j1"}, {"job_id": "j2"}]
        )
        return loop_thread, out

    loop_thread, out = asyncio.run(run())
    assert [r["error"] for r in out] == ["validation_error", "validation_error"]
    assert len(seen) == 2 and loop_thread not in seen


def _ingress_accepts(body):
    from serving.inference_pipeline.app import _validate_payload

    return _validate_payload(body)


def test_the_ingress_lets_an_entity_only_request_reach_the_online_lookup(tmp_path, monkeypatch):
    """The ingress 422'd any request without `embedding` before FeatureTransformer ran, so the
    entity-only path above was reachable from a unit test and from no HTTP client."""
    from examlops import feature_store as fs

    _features_dir(tmp_path, monkeypatch)
    fs.apply_view(fs.FeatureView("jobs", "job", ["embedding"]))
    fs.ingest("jobs", "job-42", "2026-09-01 10:00:00", {"embedding": [0.1, 0.2, 0.3]})
    fs.materialize("jobs")
    body = {"job_id": "job-42", "num_nodes": 1}
    ok, errors = _ingress_accepts(body)
    assert ok, errors
    assert _transform(body)["features"] == {"embedding": [0.1, 0.2, 0.3]}


def test_the_ingress_field_list_comes_from_the_serving_view(tmp_path, monkeypatch):
    d = tmp_path / "features"
    d.mkdir()
    (d / "v.yaml").write_text(
        "name: v\nentity: job\nentity_key: job_id\nserving: true\n"
        "features:\n  - {name: power, dtype: float}\n"
    )
    monkeypatch.setenv("EXAMLOPS_FEATURES_DIR", str(d))
    # The view declares `power`, not `embedding`: the ingress asks for what the view asks for.
    assert _ingress_accepts({"power": 1.5, "num_nodes": 1})[0] is True
    ok, errors = _ingress_accepts({"embedding": [0.1], "num_nodes": 1})
    assert ok is False and "power" in errors[0]
    # Online lookup off: naming the entity no longer stands in for the features.
    monkeypatch.setenv("EXAMLOPS_SERVING_ONLINE_FEATURES", "0")
    assert _ingress_accepts({"job_id": "j", "num_nodes": 1})[0] is False


def test_without_a_serving_view_the_ingress_keeps_the_legacy_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_FEATURES_DIR", str(tmp_path / "empty"))
    assert _ingress_accepts({"job_id": "j", "num_nodes": 1})[0] is False
    assert _ingress_accepts({"embedding": [0.1], "num_nodes": 1})[0] is True
