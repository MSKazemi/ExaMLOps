"""SLO write router — define spec / list (admin, audited, shared examlops path).

Verifies the C6 model-quality-SLO edit-parity: define/update an SLO spec through the shared
`examlops.slo.apply_spec` → `upsert_slo_spec` code path, admin + `slo.manage` gated, audited
`source=dashboard`.
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
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
        "/api/slo",
        json={"model": "JPCP", "name": "availability", "target": 0.99},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_set_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/slo",
        json={"model": "JPCP", "name": "availability", "target": 0.995, "gatePromotion": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT target, gate_promotion FROM slo_specs WHERE model='JPCP' AND name='availability'"
    ).fetchone()
    assert row == (0.995, 1)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='slo_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_set_rejects_out_of_range_target(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/slo", json={"model": "JPCP", "name": "x", "target": 1.5}, headers=h)
    assert r.status_code == 400
    r2 = await client.post("/api/slo", json={"model": "JPCP", "name": "x"}, headers=h)
    assert r2.status_code == 400  # missing target


async def test_list_returns_specs(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/slo", json={"model": "MACK", "name": "latency", "target": 0.98}, headers=h
    )
    r = await client.get("/api/slo", headers=h)
    assert r.status_code == 200
    specs = {(s["model"], s["name"]): s for s in r.json()}
    assert specs[("MACK", "latency")]["target"] == 0.98
    # Live status is best-effort, so it may be absent entirely. When present, an SLO with no
    # samples must not claim to be meeting its target: this used to assert `ok is True` and
    # called it "a trivial rollup", which is the defect — zero samples score a perfect SLI, so
    # the console rendered a green "Meeting" pill for a target nobody had measured.
    st = specs[("MACK", "latency")]["status"]
    if st is not None:
        assert st["measured"] is False
        assert st["ok"] is None


async def test_a_viewer_does_not_see_another_tenants_slo_specs(client, platform_db):
    """F15 R4: "a principal only sees its own tenant's resources unless it is a cross-tenant admin".

    `GET /api/slo` selected every row in `slo_specs` with no `WHERE` at all, and bound the
    principal to `_` so the caller's tenant was not even in scope. Every viewer of every tenant
    therefore saw every other tenant's SLO definitions — including `sli_query`, which is a
    Prometheus expression and carries that tenant's metric and label names.

    The local login is tenant `default`, so a spec seeded under another tenant must not appear.
    """
    from examlops import data as pdb

    pdb.upsert_slo_spec("JPCP", "availability", tenant="default", target=0.99)
    pdb.upsert_slo_spec(
        "SECRET-MODEL",
        "availability",
        tenant="other-centre",
        target=0.42,
        sli_query="sum(rate(other_centre_secret_metric[5m]))",
    )

    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/slo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    tenants = {row["tenant"] for row in r.json()}
    assert tenants == {"default"}, f"cross-tenant leak: a default-tenant viewer saw {tenants}"
    assert not any("other_centre_secret_metric" in (row.get("sli_query") or "") for row in r.json())


DILUTED_QUERY = (
    'sum(rate(http_requests_total{status="error"}[5m])) / sum(rate(http_requests_total[5m]))'
)


async def test_a_self_diluting_sli_is_returned_as_a_warning(client, platform_db):
    """The console must tell whoever wrote the query what the CLI would have told them.

    `apply_spec` judges the query for every surface; this asserts the dashboard actually *presents*
    the verdict rather than dropping it. Returned in the body, not logged — the person who can fix
    it is the one holding the form.
    """
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/slo",
        json={
            "model": "JPCP",
            "name": "diluted",
            "target": 0.99,
            "sliSource": "prometheus",
            "sliQuery": DILUTED_QUERY,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    warnings = r.json().get("warnings")
    assert warnings, f"the console was told nothing about a self-diluting SLI: {r.json()}"
    assert any("dilute" in w for w in warnings)
    assert any("http_requests_total" in w for w in warnings)


async def test_the_spec_is_still_written_and_the_warning_is_advisory(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/slo",
        json={
            "model": "JPCP",
            "name": "diluted-but-saved",
            "target": 0.99,
            "sliSource": "prometheus",
            "sliQuery": DILUTED_QUERY,
        },
        headers=h,
    )
    listed = await client.get("/api/slo", headers=h)
    assert listed.status_code == 200, listed.text
    assert any(s["name"] == "diluted-but-saved" for s in listed.json()), (
        "the SLO was refused rather than warned about"
    )


async def test_an_honest_query_returns_no_warnings(client, platform_db):
    """Anti-vacuity: the route must not warn about everything."""
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/slo",
        json={
            "model": "JPCP",
            "name": "honest",
            "target": 0.99,
            "sliSource": "prometheus",
            "sliQuery": 'sum(rate(a_total{s="e"}[5m])) / sum(rate(a_total{s=~"o|e"}[5m]))',
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("warnings") == []
