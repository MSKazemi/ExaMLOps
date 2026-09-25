"""Gateway live status + test-chat (P6) — proxies the deployed llm-gateway, no admin bearer held.

`GET /gateway/status` calls the service's own unauthenticated `GET /ready`; `POST /gateway/test-chat`
sends one real message through `POST /v1/chat/completions` using an operator-supplied virtual key.
Neither endpoint stores or forwards `LLM_GATEWAY_ADMIN_TOKEN` (see the router's own module docstring
for why, citing ADR 0151/`routers/health.py`).
"""

from unittest.mock import AsyncMock, patch

import dbconn
import httpx
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    pdb.init_db()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


def _mock_client(response=None, *, raises=None):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    if raises is not None:
        ctx.get = AsyncMock(side_effect=raises)
        ctx.post = AsyncMock(side_effect=raises)
    else:
        ctx.get = AsyncMock(return_value=response)
        ctx.post = AsyncMock(return_value=response)
    return ctx


def _response(status_code, json_body, text=""):
    r = AsyncMock()
    r.status_code = status_code
    r.json = lambda: json_body
    r.text = text or str(json_body)
    return r


async def test_status_reports_ready_from_the_live_service(client, platform_db):
    token = await _login(client, VIEWER_PW)
    body = {
        "ready": True,
        "healthy_now": True,
        "routes": {"default": {"healthy": True, "required": True, "deployments": 1}},
        "warnings": [],
    }
    with patch("routers.gateway.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(_response(200, body))
        r = await client.get("/api/gateway/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    out = r.json()
    assert out["reachable"] is True
    assert out["ready"] is True
    assert out["healthyNow"] is True
    assert "default" in out["routes"]


async def test_status_degrades_cleanly_when_the_gateway_is_unreachable(client, platform_db):
    """A dead gateway is a displayable state, never a dashboard 500."""
    token = await _login(client, VIEWER_PW)
    with patch("routers.gateway.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(raises=httpx.ConnectError("refused"))
        r = await client.get("/api/gateway/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    out = r.json()
    assert out == {
        "reachable": False,
        "ready": False,
        "healthyNow": False,
        "routes": {},
        "warnings": [],
    }


async def test_status_never_touches_the_admin_token(client, platform_db, monkeypatch):
    """No admin credential is read for this endpoint — ADR 0151's own boundary."""
    monkeypatch.delenv("LLM_GATEWAY_ADMIN_TOKEN", raising=False)
    token = await _login(client, VIEWER_PW)
    with patch("routers.gateway.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(_response(200, {"ready": True, "routes": {}}))
        r = await client.get("/api/gateway/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200  # would need no env var at all to succeed


async def test_test_chat_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/gateway/test-chat",
        json={"message": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_test_chat_sends_the_message_and_audits_outcome_not_content(client, platform_db):
    token = await _login(client, ADMIN_PW)
    reply_body = {"choices": [{"message": {"content": "pong"}}]}
    with patch("routers.gateway.httpx.AsyncClient") as mock_cls:
        mock_ctx = _mock_client(_response(200, reply_body))
        mock_cls.return_value = mock_ctx
        r = await client.post(
            "/api/gateway/test-chat",
            json={"message": "ping", "route": "default", "key": "exa-secret123"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True
    assert out["reply"] == "pong"
    assert isinstance(out["latencyMs"], float)

    # The upstream call carried the operator-supplied key, not a stored one.
    _, kwargs = mock_ctx.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer exa-secret123"

    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT details FROM audit_events WHERE source='dashboard' AND action='gateway_test_chat'"
    ).fetchone()
    assert row is not None
    details = row[0]
    assert "ping" not in details  # message text never audited
    assert "pong" not in details  # reply never audited
    assert "exa-secret123" not in details  # key never audited
    conn.close()


async def test_test_chat_rejects_an_empty_message_with_400(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/gateway/test-chat",
        json={"message": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


async def test_test_chat_reports_an_upstream_failure_without_a_500(client, platform_db):
    token = await _login(client, ADMIN_PW)
    with patch("routers.gateway.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(raises=httpx.ConnectError("refused"))
        r = await client.post(
            "/api/gateway/test-chat",
            json={"message": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200  # the *proxy call* succeeded; the reported outcome is failure
    out = r.json()
    assert out["ok"] is False
    assert out["code"] == "gateway_unreachable"


async def test_test_chat_surfaces_a_typed_gateway_error(client, platform_db):
    token = await _login(client, ADMIN_PW)
    err_body = {"error": {"code": "model_not_found", "message": "no such route: bogus"}}
    with patch("routers.gateway.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _mock_client(_response(404, err_body))
        r = await client.post(
            "/api/gateway/test-chat",
            json={"message": "ping", "route": "bogus"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is False
    assert out["code"] == "model_not_found"
    assert "bogus" in out["error"]
