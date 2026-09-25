"""`exa feature sync | status | materialize-due` (ADR 0017) — outcomes, not argv."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

from examlops.cli import _output  # noqa: E402
from examlops.cli.commands import feature_cmd  # noqa: E402

_app = typer.Typer(no_args_is_help=True)


@_app.callback()
def _cb(json_: bool = typer.Option(False, "--json")) -> None:
    _output.json_mode = json_


_app.add_typer(feature_cmd.app, name="feature")
runner = CliRunner()

VIEW = """\
name: jobs
entity: job
entity_key: job_id
serving: true
ttl_seconds: 7200
materialize_interval_seconds: 3600
features:
  - {name: embedding, dtype: vector, dim: 3}
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SERVING_FEATURE_TTL", "0")
    d = tmp_path / "features"
    d.mkdir()
    (d / "jobs.yaml").write_text(VIEW)
    monkeypatch.setenv("EXAMLOPS_FEATURES_DIR", str(d))
    monkeypatch.setenv("EXAMLOPS_COORDINATOR", "db")
    from examlops.feature_store.online import reset_online_store
    from examlops.feature_store.serving import reset_cache
    from examlops.platform_db import init_db

    init_db()
    reset_online_store()
    reset_cache()
    yield d
    _output.json_mode = False


def _json(args):
    result = runner.invoke(_app, ["--json", *args])
    return result, json.loads(result.output)


def test_sync_applies_the_pack_views_then_reports_unchanged():
    from examlops.feature_store import get_view

    result, doc = _json(["feature", "sync"])
    assert result.exit_code == 0, result.output
    assert [v["action"] for v in doc["views"]] == ["created"]
    assert get_view("jobs").materialize_interval_seconds == 3600
    _, again = _json(["feature", "sync"])
    assert [v["action"] for v in again["views"]] == ["unchanged"]


def test_sync_refuses_a_broken_definition_with_a_nonzero_exit(_isolated):
    (_isolated / "bad.yaml").write_text("name: bad\n")
    result = runner.invoke(_app, ["feature", "sync"])
    assert result.exit_code != 0
    from examlops.feature_store import get_view

    assert get_view("jobs") is None


def test_materialize_due_then_status_shows_a_fresh_view():
    from examlops import feature_store as fs

    _json(["feature", "sync"])
    fs.ingest("jobs", "j1", "2026-09-01 10:00:00", {"embedding": [1, 2, 3]})
    _, dry = _json(["feature", "materialize-due", "--dry-run"])
    assert dry["due"] == ["jobs"] and dry["materialized"] == []
    result, done = _json(["feature", "materialize-due"])
    assert result.exit_code == 0, result.output
    assert [m["view"] for m in done["materialized"]] == ["jobs"]
    assert fs.get_online_features("jobs", ["j1"]) == [{"embedding": [1, 2, 3]}]
    _, status = _json(["feature", "status"])
    row = next(v for v in status["views"] if v["view"] == "jobs")
    assert row["stale"] is False and row["due"] is False
    assert status["serving_view"]["view"] == "jobs"
    assert status["online_store"]["backend"] == "db"


def test_apply_interval_puts_a_hand_made_view_on_the_schedule():
    result = runner.invoke(
        _app,
        ["feature", "apply", "nodes", "--entity", "node", "--features", "cpu", "--interval", "60"],
    )
    assert result.exit_code == 0, result.output
    _, dry = _json(["feature", "materialize-due", "--dry-run"])
    assert "nodes" in dry["due"]


def test_manual_materialize_is_audited():
    from examlops import feature_store as fs
    from examlops.platform_db import get_db

    fs.apply_view(fs.FeatureView("jobs", "job", ["embedding"]))
    result = runner.invoke(_app, ["feature", "materialize", "jobs"])
    assert result.exit_code == 0, result.output
    with get_db() as conn:
        rows = conn.execute(
            "SELECT target FROM audit_events WHERE action='feature_view_materialized'"
        ).fetchall()
    assert [r["target"] for r in rows] == ["jobs"]


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    audit_mod.reset_dropped_audit_events()


def test_a_lost_manual_materialize_audit_is_counted(monkeypatch):
    from examlops import feature_store as fs
    from examlops.data.audit import dropped_audit_events

    fs.apply_view(fs.FeatureView("jobs", "job", ["embedding"]))
    _break_the_audit_log(monkeypatch)
    result = runner.invoke(_app, ["feature", "materialize", "jobs"])
    assert result.exit_code == 0, result.output
    assert dropped_audit_events()


def test_a_lost_sync_audit_is_counted_and_the_view_is_still_applied(monkeypatch):
    from examlops.data.audit import dropped_audit_events
    from examlops.feature_store import get_view
    from examlops.feature_store.definitions import sync_definitions

    _break_the_audit_log(monkeypatch)
    out = sync_definitions()
    assert [v["action"] for v in out["views"]] == ["created"]
    assert get_view("jobs") is not None
    assert dropped_audit_events()


def test_a_lost_scheduled_materialize_audit_is_counted(monkeypatch):
    from examlops import feature_store as fs
    from examlops.data.audit import dropped_audit_events
    from examlops.feature_store.scheduler import run_materialization_cycle

    fs.apply_view(fs.FeatureView("jobs", "job", ["x"], materialize_interval_seconds=60))
    fs.ingest("jobs", "j1", "2026-09-01 10:00:00", {"x": 1.0})
    _break_the_audit_log(monkeypatch)
    out = run_materialization_cycle()
    assert [m["view"] for m in out["materialized"]] == ["jobs"]
    assert dropped_audit_events()
