"""A4 — declarative asset-centric pipelines (ADR 0036)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import assets, platform_db

    platform_db.init_db()
    assets._REGISTRY.clear()
    yield
    assets._REGISTRY.clear()


def _chain():
    """dataset PM100 → feature jpcp_features → model jpcp_model."""
    from examlops.assets import declare_asset

    declare_asset("PM100", kind="dataset")
    declare_asset("jpcp_features", kind="feature", deps=["PM100"])
    declare_asset("jpcp_model", kind="model", deps=["jpcp_features"])


def test_gwt1_dag_built():
    from examlops.assets import build_dag

    _chain()
    dag = build_dag()
    assert dag["jpcp_model"] == ["jpcp_features"]
    assert dag["jpcp_features"] == ["PM100"]
    assert dag["PM100"] == []


def test_decorator_registers_asset():
    from examlops.assets import asset
    from examlops.platform_db import get_asset

    @asset("my_model", kind="model", deps=["PM100"])
    def my_model(**upstream):
        return 1

    assert get_asset("my_model") is not None
    assert get_asset("my_model")["deps"] == ["PM100"]


def test_gwt3_selective_materialize_builds_all_first_time():
    from examlops.assets import materialize

    _chain()
    result = materialize("jpcp_model", actor="tester")
    # dependencies-first order; all three are new → all rebuilt.
    assert result.rebuilt == ["PM100", "jpcp_features", "jpcp_model"]
    assert result.skipped == []


def test_gwt3_second_materialize_skips_fresh():
    from examlops.assets import materialize

    _chain()
    materialize("jpcp_model", actor="tester")
    result = materialize("jpcp_model", actor="tester")
    assert result.rebuilt == []
    assert set(result.skipped) == {"PM100", "jpcp_features", "jpcp_model"}


def test_gwt2_source_change_makes_downstream_stale():
    from examlops.assets import asset_status, mark_source_changed, materialize

    _chain()
    materialize("jpcp_model", actor="tester")
    assert asset_status("jpcp_model").fresh
    # A1 dataset revision landed → source advances.
    mark_source_changed("PM100", actor="tester")
    assert not asset_status("jpcp_features").fresh
    assert not asset_status("jpcp_model").fresh
    assert "changed" in " ".join(asset_status("jpcp_features").reasons)


def test_gwt3_selective_rebuild_after_source_change():
    from examlops.assets import mark_source_changed, materialize

    _chain()
    materialize("jpcp_model", actor="tester")
    mark_source_changed("PM100", actor="tester")
    result = materialize("jpcp_model", actor="tester")
    # PM100 was bumped externally (fresh source); features + model rebuild.
    assert "jpcp_features" in result.rebuilt
    assert "jpcp_model" in result.rebuilt


def test_never_materialized_is_stale():
    from examlops.assets import asset_status

    _chain()
    st = asset_status("jpcp_model")
    assert not st.fresh
    assert "never materialized" in st.reasons


def test_production_fn_runs_on_materialize():
    from examlops.assets import asset, declare_asset, materialize

    declare_asset("PM100", kind="dataset")
    calls = []

    @asset("jpcp_features", kind="feature", deps=["PM100"])
    def jpcp_features(**upstream):
        calls.append(upstream)

    materialize("jpcp_features", actor="tester")
    assert len(calls) == 1


def test_materialize_audited():
    from examlops.assets import materialize
    from examlops.platform_db import get_db

    _chain()
    materialize("jpcp_model", actor="tester")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='asset_materialize'"
        ).fetchall()
    assert len(rows) >= 1


def test_policy_deny_blocks_materialize(monkeypatch):
    import examlops.policy as policy_mod
    from examlops.assets import materialize
    from examlops.platform_db import get_db

    _chain()

    def _deny(action, context=None, **kw):
        return policy_mod.Decision(policy_mod.DENY, "no-assets", "assets disabled by policy")

    monkeypatch.setattr(policy_mod, "decide", _deny)
    result = materialize("jpcp_model", actor="tester")
    assert result.blocked is not None
    assert result.rebuilt == []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='asset_materialize_denied'"
        ).fetchall()
    assert len(rows) == 1


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    assert runner.invoke(app, ["assets", "declare", "PM100", "--kind", "dataset"]).exit_code == 0
    r = runner.invoke(app, ["assets", "declare", "jf", "--kind", "feature", "--deps", "PM100"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["assets", "materialize", "jf"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["assets", "status"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["assets", "graph"])
    assert r.exit_code == 0, r.output
    assert "jf" in r.output
