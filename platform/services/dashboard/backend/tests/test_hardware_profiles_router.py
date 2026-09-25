"""Hardware Profiles router (ADR 0157 Phase 4) + the workbench create form's profile (Phase 2).

Verifies the dashboard reaches the same ``examlops.hardware_profiles`` code path as the CLI:
viewer reads (catalog, show, resolve, ledger, in-use), admin-only audited writes, the shared
validation messages, the dangling-label/``inUse`` delete report, and that a workbench created
from the dashboard with a profile is sized and ledgered exactly like ``exa workbench create``.
"""

import pytest

from examlops import hardware_profiles as hp
from examlops import platform_db as pdb
from examlops.data import hardware_profiles as data
from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    pdb.create_project("research", created_by="alice")
    hp.create_profile_version(
        "gpu-small",
        accelerator_family="nvidia",
        gpu_count=1,
        cpu=4,
        memory_gb=16,
        applicability=("training", "workbench"),
    )
    hp.create_profile_version(
        "serve-only", accelerator_family="cpu", cpu=2, applicability=("serving",)
    )
    return str(db)


async def _hdr(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _audit_actions(db: str) -> list[str]:
    with pdb.get_db() as c:
        return [r["action"] for r in c.execute("SELECT action FROM audit_events ORDER BY id")]


async def test_viewer_lists_the_catalog_filtered_by_applicability(client, platform_db):
    h = await _hdr(client, VIEWER_PW)
    r = await client.get("/api/v1/hardware-profiles", headers=h)
    assert r.status_code == 200
    assert sorted(p["name"] for p in r.json()) == ["gpu-small", "serve-only"]
    r = await client.get("/api/v1/hardware-profiles?applicability=workbench", headers=h)
    assert [p["name"] for p in r.json()] == ["gpu-small"]
    assert r.json()[0]["memoryGb"] == 16.0
    bad = await client.get("/api/v1/hardware-profiles?applicability=gpu", headers=h)
    assert bad.status_code == 400


async def test_show_returns_versions_and_404s_unknown(client, platform_db):
    hp.create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=2)
    h = await _hdr(client, VIEWER_PW)
    r = await client.get("/api/v1/hardware-profiles/gpu-small", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2 and body["gpuCount"] == 2
    assert [v["version"] for v in body["versions"]] == [2, 1]
    r1 = await client.get("/api/v1/hardware-profiles/gpu-small?version=1", headers=h)
    assert r1.json()["gpuCount"] == 1
    missing = await client.get("/api/v1/hardware-profiles/nope", headers=h)
    assert missing.status_code == 404


async def test_resolve_against_unknown_cluster_is_an_unresolvable_answer(client, platform_db):
    h = await _hdr(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/hardware-profiles/gpu-small/resolve?cluster=does-not-exist", headers=h
    )
    assert r.status_code == 200
    assert r.json()["status"] == "unresolvable"
    # a look is not a use: nothing is ledgered
    assert data.list_resolutions() == []


async def test_viewer_cannot_write(client, platform_db):
    h = await _hdr(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/hardware-profiles",
        json={"name": "x", "acceleratorFamily": "cpu"},
        headers=h,
    )
    assert r.status_code == 403
    d = await client.delete("/api/v1/hardware-profiles/gpu-small", headers=h)
    assert d.status_code == 403
    assert hp.list_versions("gpu-small")  # untouched


async def test_admin_set_creates_a_version_and_audits(client, platform_db):
    h = await _hdr(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/hardware-profiles",
        json={
            "name": "gpu-small",
            "acceleratorFamily": "nvidia",
            "gpuCount": 2,
            "gpuFraction": 0.5,
            "cpu": 8,
            "applicability": ["training"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2
    assert hp.get_profile("gpu-small").gpu_count == 2  # active moved
    assert "hardware_profile_set" in _audit_actions(platform_db)


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"acceleratorFamily": "nvidia"}, "name is required"),
        ({"name": "x"}, "acceleratorFamily is required"),
        ({"name": "Bad Name", "acceleratorFamily": "nvidia"}, "slug"),
        ({"name": "x", "acceleratorFamily": "quantum"}, "accelerator_family"),
        ({"name": "x", "acceleratorFamily": "nvidia", "nodes": 0}, "nodes"),
        ({"name": "x", "acceleratorFamily": "nvidia", "gpuCount": "two"}, "gpuCount"),
        ({"name": "x", "acceleratorFamily": "nvidia", "gpuCount": 1.5}, "whole number"),
        ({"name": "x", "acceleratorFamily": "nvidia", "applicability": []}, "applicability"),
    ],
)
async def test_admin_set_refuses_invalid_input(client, platform_db, payload, fragment):
    h = await _hdr(client, ADMIN_PW)
    r = await client.post("/api/v1/hardware-profiles", json=payload, headers=h)
    assert r.status_code == 400
    assert fragment in r.json()["detail"]
    assert "x" not in hp.list_names()


async def test_admin_delete_version_reports_dangling_label_and_consumers(client, platform_db):
    from examlops import workbenches as wb

    wb.create_workbench("nb", "research", hardware_profile="gpu-small")
    wb.start_workbench("nb", "research")
    h = await _hdr(client, ADMIN_PW)
    r = await client.delete("/api/v1/hardware-profiles/gpu-small?version=1", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == 1 and body["danglingActive"] is True
    assert body["inUse"] == [{"consumer": "workbench", "consumerRef": "research/nb", "version": 1}]
    assert "hardware_profile_deleted" in _audit_actions(platform_db)
    # and the consumer now reports `missing` through the in-use surface
    iu = await client.get("/api/v1/hardware-profiles/in-use", headers=h)
    assert iu.json()["attention"][0]["status"] == "missing"


async def test_delete_unknown_404s(client, platform_db):
    h = await _hdr(client, ADMIN_PW)
    assert (await client.delete("/api/v1/hardware-profiles/nope", headers=h)).status_code == 404
    r = await client.delete("/api/v1/hardware-profiles/gpu-small?version=9", headers=h)
    assert r.status_code == 404


async def test_history_is_filtered_and_bounded(client, platform_db):
    hp.resolve_for("gpu-small", "training", consumer_ref="JPCP", project="research")
    hp.resolve_for("gpu-small", "training", consumer_ref="MACK", project="other")
    h = await _hdr(client, VIEWER_PW)
    r = await client.get("/api/v1/hardware-profiles/history?project=research", headers=h)
    assert [row["consumer_ref"] for row in r.json()] == ["JPCP"]
    assert (
        await client.get("/api/v1/hardware-profiles/history?limit=5000", headers=h)
    ).status_code == 422
    assert (
        await client.get("/api/v1/hardware-profiles/history?consumer=notebook", headers=h)
    ).status_code == 400


async def test_dashboard_workbench_create_uses_the_profile(client, platform_db):
    h = await _hdr(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/workbenches",
        json={"project": "research", "name": "nb1", "hardwareProfile": "gpu-small", "cpu": 8},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # explicit cpu wins, memory comes from the profile — the CLI's rule
    assert (body["cpu"], body["memoryGb"]) == (8, 16.0)
    assert (body["hardwareProfile"], body["hardwareProfileVersion"]) == ("gpu-small", 1)
    assert [r["consumer_ref"] for r in data.list_resolutions(consumer="workbench")] == [
        "research/nb1"
    ]


async def test_ledger_reads_obey_project_tenancy(client, platform_db, monkeypatch):
    """With EXAMLOPS_MULTITENANCY on, a session sees only the ledger rows of projects it may read.

    The legacy viewer holds only the migration grants on ``default`` (ADR 0014 decision 5), so
    ``research`` rows — its models, workbenches and who ran them — are refused when asked for by
    name and narrowed out (in SQL, before the limit) when not; rows with no project count as
    ``default``, the rule ``capabilities.tenant_visible`` applies to a resource with no tenant.
    """
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    monkeypatch.delenv("EXAMLOPS_OPENFGA_URL", raising=False)
    pdb.create_project("default", created_by="alice")
    res = hp.resolve_profile("gpu-small")
    hp.record_resolution(res, consumer="serving", consumer_ref="JPCP")  # no project
    # newest row belongs to research: limit=1 must still return the visible row, not an empty page
    hp.record_resolution(res, consumer="training", consumer_ref="SECRET", project="research")
    h = await _hdr(client, VIEWER_PW)

    denied = await client.get("/api/v1/hardware-profiles/history?project=research", headers=h)
    assert denied.status_code == 403
    denied = await client.get("/api/v1/hardware-profiles/in-use?project=research", headers=h)
    assert denied.status_code == 403

    hist = await client.get("/api/v1/hardware-profiles/history?limit=1", headers=h)
    assert [r["consumer_ref"] for r in hist.json()] == ["JPCP"]
    used = await client.get("/api/v1/hardware-profiles/in-use", headers=h)
    assert [e["consumer_ref"] for e in used.json()["entries"]] == ["JPCP"]

    # tenancy off: nothing changes for a single-tenant deployment
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY")
    hist = await client.get("/api/v1/hardware-profiles/history", headers=h)
    assert sorted(r["consumer_ref"] for r in hist.json()) == ["JPCP", "SECRET"]


async def test_dashboard_workbench_create_refuses_a_non_workbench_profile(client, platform_db):
    h = await _hdr(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/workbenches",
        json={"project": "research", "name": "nb1", "hardwareProfile": "serve-only"},
        headers=h,
    )
    assert r.status_code == 400
    assert "applicability" in r.json()["detail"]
