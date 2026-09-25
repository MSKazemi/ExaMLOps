"""ADR 0093 decision 3 + ADR 0092 decision 1 — the anatomy endpoint is a partial-tolerant BFF view
whose pipeline cards are hydrated live through the same code path as ``exa project pipelines``.

Each test asserts what the client receives: a broken section degrades to its empty shape and is
named under ``_partial`` (never a 500), and the Prefect / Ray Serve cards carry the live surface
when the deployment names those services, the registry view when it does not.
"""

import pytest
import routers.projects as projects_router

from examlops import platform_db as pdb
from examlops import project_pipelines as pp
from tests.conftest import VIEWER_PW


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    for var in (
        "PREFECT_URL",
        "PREFECT_API_URL",
        "RAY_SERVE_URL",
        "EXAMLOPS_PROJECT_PIPELINES_LIVE",
    ):
        monkeypatch.delenv(var, raising=False)
    pdb.init_db()
    pdb.create_project("demo", storage_gb=100.0)
    pdb.assign_resource_to_project("demo", "model", "JPCP")
    pdb.ensure_project_storage("demo")
    pdb.upsert_project_pipeline("demo", "prefect", "project:demo", status="healthy")
    return str(db)


async def _get(client, name="demo"):
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    token = r.json()["token"]
    return await client.get(
        f"/api/v1/projects/{name}", headers={"Authorization": f"Bearer {token}"}
    )


@pytest.fixture
def fake_live(monkeypatch):
    """Faithful fakes for the two network fetchers; records the URLs they were handed."""
    seen: dict = {}

    def prefect(project, src, *, client=None):
        seen["prefect"] = src.prefect_api_url
        return {
            "source": "live",
            "ref": f"project:{project}",
            "deployments": ["examlops-jpcp-nightly"],
            "deployment_details": [],
            "schedule": "0 2 * * *",
            "work_pool": "hpc",
            "last_run_at": "2026-09-24T02:00:00Z",
            "last_run_state": "COMPLETED",
            "last_run_deployment": "examlops-jpcp-nightly",
            "status": "healthy",
            "truncated": False,
        }

    def serve(project, models, src, *, client=None):
        seen["serve"] = src.serve_url
        return {
            "source": "live",
            "ref": pp.SERVE_APP,
            "served": ["JPCP"],
            "unserved": [],
            "aliases": {"JPCP": ["Production"]},
            "health": {"JPCP": "ok"},
            "status": "healthy",
        }

    monkeypatch.setattr(pp, "fetch_prefect_surface", prefect)
    monkeypatch.setattr(pp, "fetch_rayserve_surface", serve)
    return seen


@pytest.mark.asyncio
async def test_pipelines_hydrated_live_when_deployment_names_the_services(
    client, seeded, fake_live, monkeypatch
):
    monkeypatch.setenv("PREFECT_URL", "http://orchestrator:4200")
    monkeypatch.setenv("RAY_SERVE_URL", "http://ray-serving:8001")
    r = await _get(client)
    assert r.status_code == 200
    body = r.json()
    assert fake_live == {
        "prefect": "http://orchestrator:4200/api",
        "serve": "http://ray-serving:8001",
    }
    pf, ry = body["pipelines"]["prefect"], body["pipelines"]["rayserve"]
    assert pf["source"] == "live" and pf["deployments"] == ["examlops-jpcp-nightly"]
    assert pf["workPool"] == "hpc" and pf["lastRunState"] == "COMPLETED"
    assert pf["storagePrefix"] == "s3://examlops-projects/demo/"
    assert ry["source"] == "live" and ry["aliases"] == {"JPCP": ["Production"]}
    assert "_partial" not in body


@pytest.mark.asyncio
async def test_no_configured_services_means_registry_view_and_no_contact(client, seeded, fake_live):
    r = await _get(client)
    assert r.status_code == 200
    assert fake_live == {}
    pf = r.json()["pipelines"]["prefect"]
    assert pf["source"] == "registry" and pf["status"] == "healthy"


@pytest.mark.asyncio
async def test_unreachable_prefect_degrades_the_card_not_the_page(client, seeded, monkeypatch):
    monkeypatch.setenv("PREFECT_URL", "http://orchestrator:4200")

    def down(project, src, *, client=None):
        raise pp.LiveSourceError("Prefect answered HTTP 503")

    monkeypatch.setattr(pp, "fetch_prefect_surface", down)
    r = await _get(client)
    assert r.status_code == 200
    pf = r.json()["pipelines"]["prefect"]
    assert pf["source"] == "registry" and pf["status"] == "healthy"
    assert "HTTP 503" in pf["liveError"]


@pytest.mark.asyncio
async def test_a_broken_section_is_named_under_partial(client, seeded, monkeypatch):
    def broken(conn, name):
        raise RuntimeError("connections table unreadable")

    monkeypatch.setattr(projects_router, "_connections", broken)
    r = await _get(client)
    assert r.status_code == 200
    body = r.json()
    assert body["_partial"] == ["connections"]
    assert body["connections"] == []
    # every other section still rendered
    assert body["storage"]["bucket"] == "examlops-projects"
    assert body["resources"]["model"] == ["JPCP"]


@pytest.mark.asyncio
async def test_a_broken_pipelines_source_falls_back_to_the_registry(client, seeded, monkeypatch):
    def broken(name):
        raise RuntimeError("live overlay exploded")

    monkeypatch.setattr(projects_router, "_pipelines_live", broken)
    r = await _get(client)
    assert r.status_code == 200
    body = r.json()
    assert body["_partial"] == ["pipelines"]
    assert body["pipelines"]["prefect"]["status"] == "healthy"  # the registry row
    assert body["pipelines"]["rayserve"]["models"] == ["JPCP"]


@pytest.mark.asyncio
async def test_unknown_project_is_still_404(client, seeded):
    r = await _get(client, "ghost")
    assert r.status_code == 404
