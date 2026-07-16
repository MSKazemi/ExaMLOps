"""P5 — Project Workbenches (ADR 0090, spec P5).

GWT-1 create registers a project resource · GWT-2 start returns launch spec with mounted volume ·
GWT-3 connection env injection · lifecycle start/stop/delete.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import connections as conn  # noqa: E402
from examlops import workbenches as wb  # noqa: E402
from examlops.platform_db import create_project, init_db, list_project_resources  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", "3jZ8n4bQ6h3nJh5m3nJh5m3nJh5m3nJh5m3nJh5m3nI=")
    init_db()
    create_project("research")


def test_create_unknown_project_raises():
    with pytest.raises(wb.WorkbenchError):
        wb.create_workbench("nb", "ghost")


def test_gwt1_create_registers_storage_resource():
    wb.create_workbench("nb", "research", created_by="alice")
    got = wb.get_workbench("nb", "research")
    assert got["status"] == "STOPPED"
    assert got["storage_volume"] == "research-nb-data"
    assert list_project_resources("research")["storage"] == ["workbench:nb"]


def test_gwt2_start_returns_launch_spec_with_volume():
    wb.create_workbench("nb", "research")
    spec = wb.start_workbench("nb", "research")
    assert spec["status"] == "RUNNING"
    assert spec["volume"] == "research-nb-data"
    assert spec["image"]
    assert wb.get_workbench("nb", "research")["status"] == "RUNNING"


def test_gwt3_connection_env_injection():
    conn.create_connection(
        "minio", "s3", project="research", config={"bucket": "data"}, secret_value="s3cr3t"
    )
    wb.create_workbench("nb", "research")
    spec = wb.start_workbench("nb", "research")
    env = spec["env"]
    assert env["EXA_CONN_MINIO_BUCKET"] == "data"
    assert env["EXA_CONN_MINIO_SECRET"] == "s3cr3t"


def test_connection_env_no_connections_is_empty():
    assert wb.connection_env("research") == {}


def test_stop_and_delete_lifecycle():
    wb.create_workbench("nb", "research")
    wb.start_workbench("nb", "research")
    assert wb.stop_workbench("nb", "research") is True
    assert wb.get_workbench("nb", "research")["status"] == "STOPPED"
    assert wb.delete_workbench("nb", "research") is True
    assert wb.get_workbench("nb", "research") is None
    assert "storage" not in list_project_resources("research")


def test_start_unknown_raises():
    with pytest.raises(wb.WorkbenchError):
        wb.start_workbench("ghost", "research")
