"""P2 — Named Connections (ADR 0087, spec P2).

GWT-1 create → row + project resource · GWT-2 secret stored in secrets client, not DB ·
GWT-3 resolve injects secret · GWT-4 list never leaks secret values · deletion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import connections as conn  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    create_project,
    get_db,
    init_db,
    list_project_resources,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    # deterministic Fernet key so the secrets client works offline
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", "3jZ8n4bQ6h3nJh5m3nJh5m3nJh5m3nJh5m3nJh5m3nI=")
    init_db()
    create_project("research")


def test_gwt1_create_registers_project_resource():
    conn.create_connection("minio", "s3", project="research", config={"bucket": "data"})
    got = conn.get_connection("minio", project="research")
    assert got is not None
    assert got["kind"] == "s3"
    assert got["config"]["bucket"] == "data"
    assert list_project_resources("research")["connection"] == ["minio"]


def test_unknown_kind_rejected():
    with pytest.raises(conn.ConnectionError):
        conn.create_connection("x", "ftp", project="research")


def test_gwt2_secret_stored_in_client_not_db():
    conn.create_connection(
        "minio", "s3", project="research", config={"bucket": "b"}, secret_value="s3cr3t"
    )
    got = conn.get_connection("minio", project="research")
    assert got["secret_ref"]  # a reference is kept
    # the plaintext must NOT be present anywhere in the connections row
    with get_db() as c:
        row = c.execute("SELECT config_json, secret_ref FROM connections").fetchone()
    assert "s3cr3t" not in (row["config_json"] or "")
    assert "s3cr3t" not in (row["secret_ref"] or "")


def test_gwt3_resolve_injects_secret():
    conn.create_connection(
        "minio", "s3", project="research", config={"bucket": "b"}, secret_value="s3cr3t"
    )
    resolved = conn.resolve_connection("minio", project="research")
    assert resolved["bucket"] == "b"
    assert resolved["secret"] == "s3cr3t"
    assert resolved["kind"] == "s3"


def test_resolve_unknown_raises():
    with pytest.raises(conn.ConnectionError):
        conn.resolve_connection("ghost", project="research")


def test_gwt4_list_never_leaks_secret_value():
    conn.create_connection(
        "minio", "s3", project="research", config={"bucket": "b"}, secret_value="TOPSECRET"
    )
    rows = conn.list_connections()
    blob = str(rows)
    assert "TOPSECRET" not in blob
    assert rows[0]["has_secret"] is True


def test_global_connection_no_project():
    conn.create_connection("zenodo", "uri", config={"uri": "https://example/rec"})
    got = conn.get_connection("zenodo")
    assert got["project"] == ""


def test_delete_removes_row_and_resource():
    conn.create_connection("minio", "s3", project="research", config={"bucket": "b"})
    assert conn.delete_connection("minio", project="research") is True
    assert conn.get_connection("minio", project="research") is None
    assert "connection" not in list_project_resources("research")
    assert conn.delete_connection("minio", project="research") is False


def test_test_connection_unknown_returns_not_ok():
    result = conn.test_connection("ghost", project="research")
    assert result["ok"] is False


def test_kinds_include_dataplane_connector_kinds():
    assert {"s3", "uri", "dataplane", "sql", "kafka", "rest", "zenodo", "fs"} <= set(conn.kinds())


def test_create_accepts_a_registry_kind():
    conn.create_connection("lab-pg", "sql", config={"url": "sqlite:///x.db"})
    assert conn.get_connection("lab-pg")["kind"] == "sql"


def test_probe_delegates_to_the_connector(tmp_path):
    db = tmp_path / "p.db"
    conn.create_connection("lite", "sql", config={"url": f"sqlite:///{db}"})
    assert conn.test_connection("lite")["ok"] is True
