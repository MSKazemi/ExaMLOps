"""ADR 0014 decision 3: every platform.db table is either project-scoped or knowingly exempt.

The guard: a new table with no tenant/project/model column and no entry in ``EXEMPT`` fails here,
so cross-tenant data cannot be introduced by accident. Read-only (schema introspection only).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops.authz import scope_audit
from examlops.cli.main import app
from examlops.resilience.db import connect

runner = CliRunner()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops.platform_db import init_db

    init_db()
    return tmp_path / "platform.db"


def test_the_real_schema_has_no_unclassified_table(db):
    report = scope_audit.audit_scope()
    assert report["unscoped"] == [], (
        "new platform.db table(s) with no project/tenant/model column: "
        f"{report['unscoped']}. Add a `project` or `tenant` column, or list the table in "
        "examlops.authz.scope_audit.EXEMPT with a kind and a reason."
    )
    assert report["stale_exemptions"] == []
    assert report["ok"] is True
    # The report is honest about what is NOT partitioned today.
    assert "dataset_revisions" in report["known_gaps"]
    assert report["summary"]["scoped"] >= 40 and report["total"] >= 100


def _with_schema(monkeypatch, extra: dict[str, list[str]]):
    real = scope_audit._schema()
    monkeypatch.setattr(scope_audit, "_schema", lambda: {**real, **extra})


def test_a_new_unscoped_table_is_caught(db, monkeypatch):
    _with_schema(monkeypatch, {"customer_notes": ["id", "body"]})
    report = scope_audit.audit_scope()
    assert report["unscoped"] == ["customer_notes"] and report["ok"] is False


def test_adding_a_project_column_fixes_it(db, monkeypatch):
    _with_schema(monkeypatch, {"customer_notes": ["id", "project", "body"]})
    assert scope_audit.audit_scope()["ok"] is True


def test_a_stale_exemption_is_caught():
    schema = {"assets": ["name", "project"]}  # now scoped, but still listed as an exemption
    report = scope_audit.audit_scope(schema)
    assert "assets" in report["stale_exemptions"] and report["ok"] is False


def test_classification_rules():
    assert scope_audit.classify("t", ["id", "tenant"])[0] == "scoped"
    assert scope_audit.classify("t", ["id", "project"])[0] == "scoped"
    assert scope_audit.classify("t", ["id", "model"])[0] == "model-scoped"
    assert scope_audit.classify("coord_locks", ["key"])[0] == "exempt"
    assert scope_audit.classify("brand_new", ["id"])[0] == "UNSCOPED"


def test_every_exemption_names_a_kind_and_a_reason():
    kinds = {"global", "security", "root", "child", "gap"}
    for table, (kind, reason) in scope_audit.EXEMPT.items():
        assert kind in kinds and reason.strip(), table


def test_cli_reports_and_exits_zero_on_a_clean_schema(db):
    res = runner.invoke(app, ["--json", "project", "scope-audit"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["ok"] and doc["unscoped"] == []


def test_cli_exits_1_on_an_unscoped_table(db, monkeypatch):
    _with_schema(monkeypatch, {"leaky": ["id", "secret"]})
    res = runner.invoke(app, ["project", "scope-audit"])
    assert res.exit_code == 1 and "UNSCOPED: leaky" in res.output


def test_scope_audit_is_read_only(db):
    with connect(str(db)) as conn:
        before = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    runner.invoke(app, ["project", "scope-audit"])
    with connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == rows
