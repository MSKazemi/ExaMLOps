"""Secrets write router — set / list (admin, audited, shared examlops path; value NEVER returned).

Verifies the D7 secrets edit-parity: set/update an encrypted secret through the shared
`examlops.secrets.set_secret` code path (local Fernet store, offline), admin + `secrets.manage`
gated, audited `source=dashboard`. Hard rule: the plaintext value is never returned or listed.
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    # DASHBOARD_SECRET_KEY (a Fernet key) is set by conftest; examlops.secrets uses it as the
    # legacy keyring fallback so encrypt/decrypt works fully offline (no Vault).
    from examlops import data as pdb

    # force=False on purpose: the DDL is cached per engine (SQLite: this tmp path, never seen
    # before; Postgres: this schema, already built), and re-running 127 CREATE TABLEs per test
    # cost ~30s each there. Row isolation is the autouse fixture in conftest, not the DDL.
    pdb.init_db()
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
    conn = dbconn.connect(platform_db, row_factory=None)
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


async def test_a_viewer_does_not_see_another_tenants_secret_paths(client, platform_db):
    """The value was never exposed here; the *path list* was, to every tenant.

    `GET /api/secrets` selected every row of `secrets_store` with no tenant filter and bound the
    principal to `_`. The paths a tenant stores secrets under, their versions and who last changed
    them are that tenant's business — and a path is often the most descriptive thing about a secret
    (`prod/centre-b/db-root`). Fixed by scoping the rows to the caller (F15 R4).
    """
    from examlops import secrets as sec

    sec.set_secret("mine/token", "a", tenant="default")
    sec.set_secret("other-centre/prod/db-root", "b", tenant="other-centre")

    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/secrets", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    paths = {row["path"] for row in r.json()}
    tenants = {row["tenant"] for row in r.json()}
    assert tenants == {"default"}, f"cross-tenant leak: a default viewer saw tenants {tenants}"
    assert "other-centre/prod/db-root" not in paths
    assert "mine/token" in paths, "scoping must not empty the caller's own list"
