"""BFF aggregation substrate + /api/v1/overview endpoint (F8 / ADR 0058)."""

import asyncio

import pytest
from bff import aggregate

from tests.conftest import VIEWER_PW


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── aggregate() substrate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregate_all_ok_merges_payload():
    async def a():
        return {"x": 1}

    async def b():
        return {"y": 2}

    out = await aggregate({"a": a, "b": b})
    assert out == {"a": {"x": 1}, "b": {"y": 2}}
    assert "_partial" not in out


@pytest.mark.asyncio
async def test_aggregate_failed_source_becomes_partial():
    async def ok():
        return 1

    async def boom():
        raise RuntimeError("upstream down")

    out = await aggregate({"ok": ok, "boom": boom})
    assert out["ok"] == 1
    assert "boom" not in out
    assert out["_partial"] == ["boom"]


@pytest.mark.asyncio
async def test_aggregate_timeout_becomes_partial():
    async def slow():
        await asyncio.sleep(1.0)
        return "never"

    async def fast():
        return "ok"

    out = await aggregate({"slow": slow, "fast": fast}, timeout=0.05)
    assert out["fast"] == "ok"
    assert out["_partial"] == ["slow"]


# ── /api/v1/overview endpoint ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_requires_auth(client):
    r = await client.get("/api/v1/overview")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_overview_returns_view_shape(client):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    # meta always succeeds; it is view-shaped, not a raw upstream dump.
    assert body["meta"] == {"service": "dashboard-bff", "api": "v1"}


@pytest.mark.asyncio
async def test_overview_partial_on_source_failure(client, monkeypatch):
    from routers import bff as bff_router

    async def boom():
        raise RuntimeError("down")

    monkeypatch.setitem(bff_router.OVERVIEW_SOURCES, "drift", boom)
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "drift" not in body
    assert "drift" in body.get("_partial", [])
