"""Software-defined asset views (ADR 0036 clause 5) — read-only DAG + freshness.

The page's freshness comes from the same `examlops.assets.asset_status` as `exa assets status`, so
the two cannot disagree; the router deliberately exposes no materialize action.
"""

import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    pdb.init_db()
    return str(db)


async def _h(client):
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed():
    from examlops import assets

    assets.declare_asset("dataset:FData", kind="dataset")
    assets.declare_asset("jobs_features", kind="feature", deps=["dataset:FData"])
    assets.declare_asset("jpcp", kind="model", deps=["jobs_features", "dataset:ghost"])
    assets.materialize("jobs_features")  # builds FData then features (local orchestrator)
    assets.mark_source_changed("dataset:FData")  # → features now stale


async def test_graph_reports_freshness_from_the_shared_status(client, platform_db):
    _seed()
    r = await client.get("/api/assets", headers=await _h(client))
    assert r.status_code == 200, r.text
    body = r.json()
    by = {a["name"]: a for a in body["assets"]}
    assert set(by) == {"dataset:FData", "jobs_features", "jpcp"}
    from examlops.assets import asset_status

    for name, a in by.items():  # the page can never disagree with `exa assets status`
        st = asset_status(name)
        assert (a["fresh"], a["reasons"], a["version"]) == (st.fresh, st.reasons, st.version)
    assert by["jobs_features"]["fresh"] is False
    assert any("dataset:FData" in reason for reason in by["jobs_features"]["reasons"])
    assert by["dataset:FData"]["dependents"] == ["jobs_features"]
    assert by["jpcp"]["undeclaredDeps"] == ["dataset:ghost"]
    assert body["counts"]["total"] == 3 and body["counts"]["stale"] >= 1


async def test_detail_and_unknown_asset(client, platform_db):
    _seed()
    h = await _h(client)
    r = await client.get("/api/assets/jobs_features", headers=h)
    assert r.status_code == 200 and r.json()["deps"] == ["dataset:FData"]
    assert (await client.get("/api/assets/nope", headers=h)).status_code == 404


async def test_requires_login_and_offers_no_write(client, platform_db):
    assert (await client.get("/api/assets")).status_code == 401
    from routers import assets as router_mod

    methods = {m for route in router_mod.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}  # views only: rebuilding stays `exa assets materialize`
