"""ADR 0036 — the platform declares its own assets, so the DAG is not empty.

The recorded finding: "nothing in the platform declares an asset on its own — no dataset revision,
feature view, pipeline run or model registration registers one — so the DAG is empty until an
operator types it in, which is the opposite of the declarative substrate the ADR describes."

A dataset revision is the platform's most common source event, and `mark_source_changed` was
written for exactly it — its docstring says "e.g. an A1 dataset revision landed" — and was called
from nowhere near it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

import examlops.data as pdb  # noqa: E402
from examlops.assets import asset_status, declare_asset  # noqa: E402
from examlops.data.data_assets import record_dataset_revision  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)
    init_db()


def _rev(revision_id="r1", dataset="PM100"):
    return SimpleNamespace(
        backend="minio",
        dataset=dataset,
        revision_id=revision_id,
        kind="content",
        uri=f"s3://bucket/{revision_id}",
        schema_hash="h",
    )


def test_recording_a_revision_declares_the_dataset_asset():
    record_dataset_revision(_rev())
    row = pdb.get_asset("PM100")
    assert row is not None and row["current_version"] == 1


def test_a_second_revision_advances_the_version():
    record_dataset_revision(_rev("r1"))
    record_dataset_revision(_rev("r2"))
    assert pdb.get_asset("PM100")["current_version"] == 2


def test_re_recording_the_same_revision_does_not_advance_it():
    """`record_dataset_revision` is idempotent on (backend, dataset, revision_id). Bumping on a
    re-record would report the dataset as changed when nothing changed, and every downstream model
    would go spuriously stale — a freshness signal that cries wolf is one nobody acts on."""
    record_dataset_revision(_rev("r1"))
    record_dataset_revision(_rev("r1"))
    record_dataset_revision(_rev("r1"))
    assert pdb.get_asset("PM100")["current_version"] == 1


def test_two_datasets_are_two_assets():
    record_dataset_revision(_rev("r1", dataset="PM100"))
    record_dataset_revision(_rev("r1", dataset="FData"))
    assert pdb.get_asset("PM100")["current_version"] == 1
    assert pdb.get_asset("FData")["current_version"] == 1


def test_a_downstream_model_goes_stale_when_new_data_lands():
    """The point of declaring it: freshness becomes automatic. A model built against revision N
    is stale the moment N+1 lands, without anyone recording that by hand."""
    record_dataset_revision(_rev("r1"))
    declare_asset("jpcp_model", kind="model", deps=["PM100"])
    from examlops.assets import materialize

    materialize("jpcp_model")
    assert asset_status("jpcp_model").fresh is True

    record_dataset_revision(_rev("r2"))
    assert asset_status("jpcp_model").fresh is False


def test_the_revision_is_still_recorded_if_the_asset_layer_fails(monkeypatch):
    """The revision is the durable fact; the asset graph is a derived view of it. Failing to
    update the view must never lose the fact."""
    import examlops.assets as assets_mod

    def _boom(*a, **k):
        raise RuntimeError("asset store is down")

    monkeypatch.setattr(assets_mod, "mark_source_changed", _boom)
    record_dataset_revision(_rev("r1"))  # must not raise

    revs = pdb.get_dataset_revisions("PM100")
    assert any(r["revision_id"] == "r1" for r in revs)


# ── feature views declare themselves too ──────────────────────────────────────


def _view(name="jpcp_features", source="PM100"):
    from examlops.feature_store import FeatureView

    return FeatureView(name=name, entity="node", features=["a", "b"], source=source)


def test_applying_a_feature_view_declares_it_as_an_asset():
    from examlops.feature_store import apply_view

    apply_view(_view())
    row = pdb.get_asset("jpcp_features")
    assert row is not None and row["kind"] == "feature"


def test_the_view_depends_on_its_source_dataset():
    """A view names both halves of the edge — an entity-level name and the source it is built
    from — at exactly the granularity assets use."""
    from examlops.assets import build_dag
    from examlops.feature_store import apply_view

    record_dataset_revision(_rev())
    apply_view(_view())
    assert build_dag()["jpcp_features"] == ["PM100"]


def test_the_graph_now_spans_data_to_features_without_anyone_typing_it():
    """The two producers together: recording data and applying a view build the DAG between
    them, which is the declarative substrate ADR 0036 asks for."""
    from examlops.assets import build_dag
    from examlops.feature_store import apply_view

    record_dataset_revision(_rev())
    apply_view(_view())
    assert build_dag() == {"PM100": [], "jpcp_features": ["PM100"]}


def test_new_data_makes_the_feature_view_stale():
    from examlops.assets import asset_status, materialize
    from examlops.feature_store import apply_view

    record_dataset_revision(_rev("r1"))
    apply_view(_view())
    materialize("jpcp_features")
    assert asset_status("jpcp_features").fresh is True

    record_dataset_revision(_rev("r2"))
    assert asset_status("jpcp_features").fresh is False


def test_changing_the_source_moves_the_edge():
    """Re-applying is idempotent and a source change is a real definition change — unlike a
    per-run derivation, these deps cannot flip-flop between runs."""
    from examlops.assets import build_dag
    from examlops.feature_store import apply_view

    apply_view(_view(source="PM100"))
    apply_view(_view(source="PM100"))
    assert build_dag()["jpcp_features"] == ["PM100"]
    apply_view(_view(source="FData"))
    assert build_dag()["jpcp_features"] == ["FData"]


def test_a_view_with_no_source_declares_a_root_asset():
    from examlops.assets import build_dag
    from examlops.feature_store import apply_view

    apply_view(_view(source=None))
    assert build_dag()["jpcp_features"] == []


def test_the_view_is_still_registered_if_the_asset_layer_fails(monkeypatch):
    import examlops.assets as assets_mod
    from examlops.feature_store import apply_view, get_view

    monkeypatch.setattr(
        assets_mod, "declare_asset", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    apply_view(_view())  # must not raise
    assert get_view("jpcp_features") is not None
