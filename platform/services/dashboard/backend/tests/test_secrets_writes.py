"""Secrets write router — set / list (admin, audited, shared examlops path; value NEVER returned).

Verifies the D7 secrets edit-parity: set/update an encrypted secret through the shared
`examlops.secrets.set_secret` code path (local Fernet store, offline), admin + `secrets.manage`
gated, audited `source=dashboard`. Hard rule: the plaintext value is never returned or listed.
"""

import sqlite3

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    # DASHBOARD_SECRET_KEY (a Fernet key) is set by conftest; examlops.secrets uses it as the
    # legacy keyring fallback so encrypt/decrypt works fully offline (no Vault).
    from examlops import data as pdb

    pdb.init_db(force=True)
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def test_set_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/secrets",
        json={"path": "svc/token", "value": "s3cr3t"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_set_stores_encrypted_and_never_returns_value(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/secrets", json={"path": "svc/token", "value": "s3cr3t"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {"path": "svc/token", "tenant": "default", "version": 1}
    assert "value" not in body and "s3cr3t" not in r.text  # value never echoed
    conn = sqlite3.connect(platform_db)
    ct = conn.execute("SELECT ciphertext FROM secrets_store WHERE path='svc/token'").fetchone()[0]
    assert ct and "s3cr3t" not in ct  # stored encrypted, not plaintext
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='secret_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_set_validates_path_and_value(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/secrets", json={"value": "x"}, headers=h)).status_code == 400
    assert (await client.post("/api/secrets", json={"path": "p"}, headers=h)).status_code == 400


async def test_list_returns_metadata_only(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/secrets", json={"path": "svc/token", "value": "s3cr3t"}, headers=h)
    r = await client.get("/api/secrets", headers=h)
    assert r.status_code == 200
    assert "s3cr3t" not in r.text  # no plaintext anywhere in the list
    row = next(s for s in r.json() if s["path"] == "svc/token")
    assert row["hasValue"] is True
    assert "value" not in row and "ciphertext" not in row
    assert row["version"] == 1
