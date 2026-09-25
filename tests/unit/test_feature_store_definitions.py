"""ADR 0017 clauses 2 + 5 — the pack's feature-view definitions and their registry mirror."""

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
timestamp_field: adt
serving: {serving}
embedding_feature: embedding
ttl_seconds: 60
materialize_interval_seconds: {interval}
features:
  - {{name: embedding, dtype: vector, dim: {dim}}}
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_SERVING_FEATURE_VIEW", raising=False)
    from examlops.platform_db import init_db

    init_db()


def _write(d: Path, name: str, *, serving="false", interval=0, dim=3) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(VIEW.format(name=name, serving=serving, interval=interval, dim=dim))
    return p


def _audit(action: str) -> list[dict]:
    from examlops.platform_db import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT target, details FROM audit_events WHERE action=? ORDER BY id", (action,)
        ).fetchall()
    return [dict(r) for r in rows]


def test_load_reports_every_broken_file_and_keeps_the_good_ones(tmp_path):
    from examlops.feature_store.definitions import load_definitions

    d = tmp_path / "features"
    _write(d, "good")
    (d / "bad.yaml").write_text("name: bad\nentity: job\nfeatures: []\n")
    (d / "dup.yaml").write_text(VIEW.format(name="good", serving="false", interval=0, dim=3))
    (d / "_draft.yaml").write_text("not: loaded")
    defs = load_definitions(d)
    assert list(defs.views) == ["good"]
    assert any("bad.yaml" in e and "non-empty" in e for e in defs.errors)
    assert any("already declared" in e for e in defs.errors)


def test_two_serving_views_is_an_error_not_a_guess(tmp_path):
    from examlops.feature_store.definitions import load_definitions

    d = tmp_path / "features"
    _write(d, "a", serving="true")
    _write(d, "b", serving="true")
    defs = load_definitions(d)
    assert any("more than one view is marked serving" in e for e in defs.errors)
    assert defs.serving_view() is None


def test_env_selects_the_serving_view(tmp_path, monkeypatch):
    from examlops.feature_store.definitions import load_definitions

    d = tmp_path / "features"
    _write(d, "a", serving="true")
    _write(d, "b")
    assert load_definitions(d).serving_view().name == "a"
    monkeypatch.setenv("EXAMLOPS_SERVING_FEATURE_VIEW", "b")
    assert load_definitions(d).serving_view().name == "b"


def test_missing_directory_is_an_empty_set(tmp_path):
    from examlops.feature_store.definitions import load_definitions

    defs = load_definitions(tmp_path / "nope")
    assert defs.views == {} and defs.errors == []


def test_sync_is_idempotent_audited_and_reports_orphans(tmp_path):
    from examlops import feature_store as fs
    from examlops.feature_store.definitions import sync_definitions

    d = tmp_path / "features"
    _write(d, "jobs", interval=3600)
    fs.apply_view(fs.FeatureView("handmade", "node", ["cpu"]))

    dry = sync_definitions(d, dry_run=True)
    assert [v["action"] for v in dry["views"]] == ["created"]
    assert fs.get_view("jobs") is None, "a dry run applied the view"

    first = sync_definitions(d, actor="tester")
    assert [v["action"] for v in first["views"]] == ["created"]
    assert first["orphans"] == ["handmade"]
    view = fs.get_view("jobs")
    assert view.materialize_interval_seconds == 3600
    assert view.spec["entity_key"] == "job_id"
    assert view.spec["fingerprint"] == first["views"][0]["fingerprint"]

    again = sync_definitions(d)
    assert [v["action"] for v in again["views"]] == ["unchanged"]
    assert len(_audit("feature_view_synced")) == 1

    _write(d, "jobs", interval=3600, dim=4)
    changed = sync_definitions(d)
    assert [v["action"] for v in changed["views"]] == ["updated"]
    assert changed["views"][0]["fingerprint"] != first["views"][0]["fingerprint"]
    assert len(_audit("feature_view_synced")) == 2
    assert fs.get_view("handmade") is not None, "sync deleted a view the pack does not declare"


def test_sync_refuses_a_broken_definition_set(tmp_path):
    from examlops import feature_store as fs
    from examlops.feature_store.definitions import sync_definitions

    d = tmp_path / "features"
    _write(d, "jobs")
    (d / "broken.yaml").write_text("name: x\n")
    with pytest.raises(ValueError, match="feature definitions are invalid"):
        sync_definitions(d)
    assert fs.get_view("jobs") is None


# ── the reference pack's concrete case (clause 5) ────────────────────────────────────────────


def test_the_reference_pack_declares_the_fdata_embedding_view():
    from examlops.feature_store.definitions import load_definitions

    defs = load_definitions(ROOT / "usecases" / "reference" / "features")
    assert defs.errors == []
    view = defs.serving_view()
    assert view is not None, "the reference pack names no serving feature view"
    emb = [f for f in view.features if f.name == view.embedding_feature]
    assert emb and emb[0].dtype == "vector" and emb[0].dim == 384
    assert view.entity_key and view.timestamp_field
    assert view.materialize_interval_seconds > 0 and view.ttl_seconds > 0


def test_every_model_binding_names_a_declared_view():
    from examlops.feature_store.definitions import check_model_bindings, load_definitions
    from pipelines.model_loader import scan_model_yamls

    pack = ROOT / "usecases" / "reference"
    bindings = [
        (m.name, ds.name, ds.feature_view)
        for m in scan_model_yamls(pack / "models")
        for ds in m.datasets
        if ds.feature_view
    ]
    assert bindings, "no model binds a feature view — clause 2 has no production caller"
    assert check_model_bindings(bindings, load_definitions(pack / "features")) == []
    assert check_model_bindings([("M", "D", "ghost")], load_definitions(pack / "features"))


def test_a_model_trained_on_a_view_serving_does_not_apply_is_a_binding_problem(tmp_path):
    """Serving's FeatureTransformer applies ONE view (the serving view) to every request. A model
    whose training gate validated a different view would be served under a definition it was
    never checked against: the train/serve skew this ADR exists to prevent."""
    from examlops.feature_store.definitions import check_model_bindings, load_definitions

    d = tmp_path / "features"
    d.mkdir()
    base = "entity: job\nfeatures:\n  - {name: x, dtype: float}\n"
    (d / "a.yaml").write_text("name: served\nserving: true\n" + base)
    (d / "b.yaml").write_text("name: other\n" + base)
    defs = load_definitions(d)
    assert check_model_bindings([("M", "D", "served")], defs) == []
    problems = check_model_bindings([("M", "D", "other")], defs)
    assert problems and "serving view 'served'" in problems[0]
