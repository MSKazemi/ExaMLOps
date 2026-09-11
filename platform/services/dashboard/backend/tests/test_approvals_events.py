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


class _ApprovalListResp:
    is_success = True
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return [{"id": "approval-1", "model_id": "JPCP", "status": "pending"}]


class _ApprovalListClient:
    headers: dict[str, str] | None = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, _url, *, params, headers):
        type(self).headers = headers
        return _ApprovalListResp()


@pytest.mark.asyncio
async def test_list_forwards_configured_control_plane_token(client, monkeypatch):
    async def configured_token(_db):
        return "configured-control-plane-token"

    monkeypatch.setattr("routers.approvals._get_control_plane_token", configured_token)
    monkeypatch.setattr("routers.approvals.httpx.AsyncClient", _ApprovalListClient)
    token = await _login(client, ADMIN_PW)

    response = await client.get(
        "/api/approvals?status=pending", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert _ApprovalListClient.headers == {"Authorization": "Bearer configured-control-plane-token"}


@pytest.mark.asyncio
async def test_list_hides_upstream_auth_error_body(client, monkeypatch):
    class _DeniedResponse:
        is_success = False
        status_code = 401
        text = "upstream credential detail must not reach the browser"

    class _DeniedClient(_ApprovalListClient):
        async def get(self, _url, *, params, headers):
            return _DeniedResponse()

    async def no_token(_db):
        return None

    monkeypatch.setattr("routers.approvals._get_control_plane_token", no_token)
    monkeypatch.setattr("routers.approvals.httpx.AsyncClient", _DeniedClient)
    token = await _login(client, ADMIN_PW)

    response = await client.get("/api/approvals", headers={"Authorization": f"Bearer {token}"})

    # Not 401: that would tell the browser *its* session is invalid, and the SPA signs the user
    # out and reloads — every page load, for as long as the service token is wrong. The control
    # plane refused the dashboard's credential, which is a bad gateway, and the message says so.
    assert response.status_code == 502
    assert "credential" in response.json()["detail"]
    assert "credential detail" not in response.text


@pytest.mark.asyncio
async def test_list_requires_dashboard_viewer_auth_before_using_service_token(client, monkeypatch):
    async def must_not_read_token(_db):
        raise AssertionError("unauthenticated requests must not resolve the service credential")

    monkeypatch.setattr("routers.approvals._get_control_plane_token", must_not_read_token)

    response = await client.get("/api/approvals")

    assert response.status_code == 401


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


@pytest.mark.asyncio
async def test_approve_and_reject_hide_upstream_error_body(client, monkeypatch):
    """D15: the POST proxies mask upstream bodies exactly like the GET does."""

    class _FailResp:
        is_success = False
        status_code = 500
        text = "upstream stack trace with internals"

    class _FailClient(_FakeAsyncClient):
        async def post(self, *a, **k):
            return _FailResp()

    async def no_token(_db):
        return None

    monkeypatch.setattr("routers.approvals._get_control_plane_token", no_token)
    monkeypatch.setattr("routers.approvals.httpx.AsyncClient", _FailClient)
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}

    r = await client.post("/api/approvals/approve/JPCP", headers=h)
    assert r.status_code == 500
    assert r.json() == {"detail": "Control Plane returned an error"}
    assert "stack trace" not in r.text

    r = await client.post("/api/approvals/reject/JPCP", json={"reason": "nope"}, headers=h)
    assert r.status_code == 500
    assert r.json() == {"detail": "Control Plane returned an error"}
    assert "stack trace" not in r.text
