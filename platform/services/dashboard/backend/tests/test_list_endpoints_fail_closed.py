"""List endpoints must not mask datastore failure as an empty list (audit finding D12).

Four viewer list endpoints wrapped their whole body in ``except Exception: return []`` —
a locked, corrupt, or unreachable datastore looked exactly like "no rows yet". Only the
missing-table case (nothing recorded on a fresh deployment) may return ``[]``; every other
failure is now a 503 with a generic message, never the raw error text.
"""

import sqlite3

import pytest

from tests.conftest import VIEWER_PW

_ENDPOINTS = {
    "connections": ("/api/v1/connections", "routers.connections"),
    "secrets": ("/api/secrets", "routers.secrets"),
    "gateway": ("/api/gateway/keys", "routers.gateway"),
    "workbenches": ("/api/v1/workbenches", "routers.workbenches"),
}


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.parametrize("path,module", _ENDPOINTS.values(), ids=_ENDPOINTS.keys())
async def test_missing_table_still_returns_empty_list(client, tmp_path, monkeypatch, path, module):
    """A fresh datastore with no tables at all is legitimately 'nothing recorded yet'."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "empty.db"))
    token = await _login(client, VIEWER_PW)
    r = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.parametrize("path,module", _ENDPOINTS.values(), ids=_ENDPOINTS.keys())
async def test_datastore_failure_returns_503_not_empty_list(client, monkeypatch, path, module):
    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked: /secret/host/path/platform.db")

    monkeypatch.setattr(f"{module}.connect", _boom)
    token = await _login(client, VIEWER_PW)
    r = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    # Generic message only — never the raw error (which can leak paths/infra detail).
    assert r.json()["detail"] == "datastore unavailable"
    assert "secret/host/path" not in r.text


@pytest.mark.parametrize("path,module", _ENDPOINTS.values(), ids=_ENDPOINTS.keys())
async def test_unexpected_failure_returns_503(client, monkeypatch, path, module):
    def _boom(*_a, **_k):
        raise RuntimeError("wal frame corrupt")

    monkeypatch.setattr(f"{module}.connect", _boom)
    token = await _login(client, VIEWER_PW)
    r = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    assert r.json()["detail"] == "datastore unavailable"
