"""Approvals router publishes live events to the realtime gateway (F8)."""

import pytest
from realtime import bus

from tests.conftest import ADMIN_PW


async def _login(client, pw):
    r = await client.post("/api/auth/login", json={"password": pw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


class _FakeResp:
    is_success = True
    status_code = 200

    @staticmethod
    def json():
        return {"status": "scheduled", "flow_run_id": "run-xyz"}


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp()


@pytest.mark.asyncio
async def test_approve_publishes_event(client, monkeypatch):
    monkeypatch.setattr("routers.approvals.httpx.AsyncClient", _FakeAsyncClient)
    sub = bus.subscribe(("approval.*",))
    try:
        token = await _login(client, ADMIN_PW)
        r = await client.post(
            "/api/approvals/approve/JPCP", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, r.text
        assert sub.queue.qsize() == 1
        evt = sub.queue.get_nowait()
        assert evt.channel == "approval.approved"
        assert evt.data["model"] == "JPCP"
    finally:
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_reject_publishes_event(client, monkeypatch):
    monkeypatch.setattr("routers.approvals.httpx.AsyncClient", _FakeAsyncClient)
    sub = bus.subscribe(("approval.*",))
    try:
        token = await _login(client, ADMIN_PW)
        r = await client.post(
            "/api/approvals/reject/JPCP",
            headers={"Authorization": f"Bearer {token}"},
            json={"reason": "needs review"},
        )
        assert r.status_code == 200, r.text
        evt = sub.queue.get_nowait()
        assert evt.channel == "approval.rejected"
        assert evt.data["reason"] == "needs review"
    finally:
        bus.unsubscribe(sub)
