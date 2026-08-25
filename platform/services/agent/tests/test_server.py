"""Tests for the FastAPI chat server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage


# Build a minimal fake graph for testing
def _fake_graph():
    state_mock = MagicMock()
    state_mock.values = {
        "messages": [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there, how can I help?"),
        ]
    }
    state_mock.tasks = []
    graph = MagicMock()
    graph.get_state.return_value = state_mock
    graph.checkpointer.list.return_value = []
    return graph


@pytest.fixture()
def client(monkeypatch):
    from skipper import config, oai_compat
    from skipper import server as srv

    # Reset shared graph state so each test starts clean
    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", "")
    monkeypatch.setattr(config, "AGENT_TENANT", "default")
    seen_actions: set[str] = set()

    def first_seen(key: str, _ttl_s: float) -> bool:
        if key in seen_actions:
            return False
        seen_actions.add(key)
        return True

    monkeypatch.setattr(oai_compat, "_first_seen_action", first_seen)
    srv._graph = None
    srv._backend_info = {}
    monkeypatch.setattr(srv, "acquire_turn", lambda _thread_id: MagicMock())
    with patch.object(srv, "_get_graph", return_value=_fake_graph()):
        with patch(
            "skipper.server.check_backend",
            return_value={"type": "claude", "model": "claude-opus-4-8", "ok": True},
        ):
            yield TestClient(srv.app)


# ── HTML endpoint ──────────────────────────────────────────────────────────────


def test_index_returns_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "ExaMLOps" in resp.text
    assert "<script" in resp.text


def test_index_contains_websocket_js(client):
    resp = client.get("/")
    assert "WebSocket" in resp.text
    assert "ws/chat" in resp.text


# ── /api/info ──────────────────────────────────────────────────────────────────


def test_api_info_returns_backend(client):
    resp = client.get("/api/info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["backend"] == "claude"
    assert data["model"] == "claude-opus-4-8"
    assert data["ok"] is True


# ── /api/threads ───────────────────────────────────────────────────────────────


def test_api_threads_empty(client):
    resp = client.get("/api/threads")
    assert resp.status_code == 200
    assert resp.json() == {"threads": []}


def test_api_threads_lists_saved(client):
    from skipper import server as srv
    from skipper.auth import local_identity, scope_thread_id

    fake = _fake_graph()
    t1 = MagicMock()
    t1.config = {"configurable": {"thread_id": scope_thread_id(local_identity(), "cli-aaa")}}
    t2 = MagicMock()
    t2.config = {"configurable": {"thread_id": scope_thread_id(local_identity(), "cli-bbb")}}
    fake.checkpointer.list.return_value = [t1, t2]
    with patch.object(srv, "_get_graph", return_value=fake):
        resp = client.get("/api/threads")
    assert set(resp.json()["threads"]) == {"cli-aaa", "cli-bbb"}


def test_thread_list_and_history_are_scoped_to_verified_principal(client, monkeypatch):
    from skipper import auth, config
    from skipper import server as srv

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", '{"alice":"key-a","bob":"key-b"}')
    monkeypatch.setattr(config, "AGENT_TENANT", "production")
    alice = auth.AgentIdentity("alice", "production")
    bob = auth.AgentIdentity("bob", "production")
    fake = _fake_graph()
    checkpoints = []
    for identity, session in ((alice, "alice-only"), (bob, "bob-only")):
        checkpoint = MagicMock()
        checkpoint.config = {"configurable": {"thread_id": auth.scope_thread_id(identity, session)}}
        checkpoints.append(checkpoint)
    fake.checkpointer.list.return_value = checkpoints

    with patch.object(srv, "_get_graph", return_value=fake):
        alice_list = client.get("/api/threads", headers={"Authorization": "Bearer key-a"}).json()
        bob_list = client.get("/api/threads", headers={"Authorization": "Bearer key-b"}).json()
        client.get(
            "/api/threads/shared-label/history",
            headers={"Authorization": "Bearer key-a"},
        )
        alice_cfg = fake.get_state.call_args[0][0]
        client.get(
            "/api/threads/shared-label/history",
            headers={"Authorization": "Bearer key-b"},
        )
        bob_cfg = fake.get_state.call_args[0][0]

    assert alice_list == {"threads": ["alice-only"]}
    assert bob_list == {"threads": ["bob-only"]}
    assert alice_cfg != bob_cfg


# ── auth boundary ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ("/api/info", "/api/threads", "/api/threads/private/history"))
def test_data_routes_require_agent_key_when_configured(client, monkeypatch, path):
    from skipper import config

    monkeypatch.setattr(config, "AGENT_API_KEY", "secret")
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer secret"}).status_code == 200


def test_browser_login_issues_cookie_for_http_and_websocket(client, monkeypatch):
    from skipper import config

    monkeypatch.setattr(config, "AGENT_API_KEY", "secret")
    login = client.post("/", data={"api_key": "secret"}, follow_redirects=False)
    assert login.status_code == 303
    cookie = login.cookies.get("examlops_agent_session")
    assert cookie
    assert cookie != "secret"
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
    assert f"Max-Age={config.AGENT_BROWSER_SESSION_TTL_SECONDS}" in login.headers["set-cookie"]

    assert client.get("/api/threads").status_code == 200
    with client.websocket_connect("/ws/chat/browser-session") as ws:
        ws.send_json({"type": "message", "text": "hello"})
        assert ws.receive_json()["type"] == "done"


def test_browser_login_rejects_wrong_key(client, monkeypatch):
    from skipper import config

    monkeypatch.setattr(config, "AGENT_API_KEY", "secret")
    response = client.post("/", data={"api_key": "wrong"})
    assert response.status_code == 401
    assert "Invalid API key" in response.text


def test_websocket_rejects_missing_agent_key(client, monkeypatch):
    from skipper import config
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(config, "AGENT_API_KEY", "secret")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat/private"):
            pass


def test_agent_key_comparison_is_constant_time(monkeypatch):
    from skipper import config, server

    monkeypatch.setattr(config, "AGENT_API_KEY", "secret")
    compared = []

    def compare(left, right):
        compared.append((left, right))
        return left == right

    monkeypatch.setattr(server.hmac, "compare_digest", compare)
    assert server._token_matches("secret") is True
    assert compared == [("secret", "secret")]


# ── /api/threads/{id}/history ─────────────────────────────────────────────────


def test_thread_history_returns_messages(client):
    resp = client.get("/api/threads/cli-test/history")
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert any(m["role"] == "human" and "Hello" in m["content"] for m in msgs)
    assert any(m["role"] == "ai" and "Hi there" in m["content"] for m in msgs)


def test_thread_history_empty_on_error(client):
    from skipper import server as srv

    bad_graph = _fake_graph()
    bad_graph.get_state.side_effect = RuntimeError("no state")
    with patch.object(srv, "_get_graph", return_value=bad_graph):
        resp = client.get("/api/threads/missing/history")
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


# ── WebSocket ──────────────────────────────────────────────────────────────────


def test_websocket_receives_done_on_empty_stream(client):
    from skipper import server as srv

    fake = _fake_graph()
    # graph.stream returns nothing (empty)
    fake.stream.return_value = iter([])

    with patch.object(srv, "_get_graph", return_value=fake):
        with client.websocket_connect("/ws/chat/test-thread") as ws:
            ws.send_json({"type": "message", "text": "hello"})
            events = []
            # Collect until "done" or error
            for _ in range(10):
                try:
                    data = ws.receive_json()
                    events.append(data)
                    if data.get("type") in ("done", "error"):
                        break
                except Exception:
                    break

    types = [e["type"] for e in events]
    assert "done" in types


@pytest.mark.parametrize(
    ("error_type", "code"),
    (("busy", "session_busy"), ("unavailable", "coordination_unavailable")),
)
def test_websocket_turn_lock_fails_closed(client, monkeypatch, error_type, code):
    from skipper import server as srv
    from skipper.turns import TurnBusy, TurnCoordinationUnavailable

    error = TurnBusy("busy") if error_type == "busy" else TurnCoordinationUnavailable("down")

    def reject(_thread_id):
        raise error

    fake = _fake_graph()
    monkeypatch.setattr(srv, "acquire_turn", reject)
    with patch.object(srv, "_get_graph", return_value=fake):
        with client.websocket_connect("/ws/chat/serialized-thread") as ws:
            ws.send_json({"type": "message", "text": "hello"})
            event = ws.receive_json()

    assert event["type"] == "error"
    assert event["code"] == code
    fake.stream.assert_not_called()


def _interrupt_state():
    intr = MagicMock()
    intr.id = "interrupt-1"
    intr.value = {"action": "delete_model", "summary": "Delete model jpcp"}
    task = MagicMock()
    task.interrupts = [intr]
    state = MagicMock()
    state.values = {"messages": []}
    state.tasks = [task]
    return state


def test_websocket_rejects_plain_text_resume_for_pending_write(client):
    from skipper import server as srv

    fake = _fake_graph()
    fake.get_state.return_value = _interrupt_state()

    with patch.object(srv, "_get_graph", return_value=fake):
        with client.websocket_connect("/ws/chat/pending-thread") as ws:
            pending = ws.receive_json()
            assert pending["type"] == "interrupt"
            assert pending["action_id"].startswith("act.")

            ws.send_json({"type": "resume", "answer": "yes"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "typed action" in error["message"]

            ws.send_json({"type": "message", "text": "yes"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert "awaiting approval" in error["message"]

    fake.stream.assert_not_called()


def test_websocket_uses_opaque_action_id_for_typed_decision(client):
    from skipper import server as srv

    pending_state = _interrupt_state()
    finished_state = MagicMock()
    finished_state.values = {"messages": []}
    finished_state.tasks = []
    fake = _fake_graph()
    fake.stream.return_value = iter([])
    fake.get_state.side_effect = [pending_state, pending_state, finished_state]

    with patch.object(srv, "_get_graph", return_value=fake):
        with client.websocket_connect("/ws/chat/pending-thread") as ws:
            pending = ws.receive_json()
            ws.send_json(
                {
                    "type": "action",
                    "action_id": pending["action_id"],
                    "decision": "approve",
                }
            )
            assert ws.receive_json()["type"] == "done"

    command = fake.stream.call_args[0][0]
    assert command.resume == "approve"


def test_websocket_thread_is_scoped_to_verified_principal(client, monkeypatch):
    from skipper import config
    from skipper import server as srv

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", '{"alice":"key-a","bob":"key-b"}')
    monkeypatch.setattr(config, "AGENT_TENANT", "production")
    fake = _fake_graph()
    fake.stream.side_effect = lambda *_a, **_k: iter([])

    with patch.object(srv, "_get_graph", return_value=fake):
        with client.websocket_connect(
            "/ws/chat/shared-label", headers={"Authorization": "Bearer key-a"}
        ) as ws:
            ws.send_json({"type": "message", "text": "hello"})
            assert ws.receive_json()["type"] == "done"
        alice_cfg = fake.stream.call_args[0][1]

        with client.websocket_connect(
            "/ws/chat/shared-label", headers={"Authorization": "Bearer key-b"}
        ) as ws:
            ws.send_json({"type": "message", "text": "hello"})
            assert ws.receive_json()["type"] == "done"
        bob_cfg = fake.stream.call_args[0][1]

    assert alice_cfg != bob_cfg
    assert alice_cfg["configurable"]["thread_id"].endswith(":rw:shared-label")


def test_websocket_streams_tokens(client):
    from langchain_core.messages import AIMessageChunk
    from skipper import server as srv

    chunk = AIMessageChunk(content="Hello world")
    fake = _fake_graph()
    fake.stream.return_value = iter([(chunk, {})])

    with patch.object(srv, "_get_graph", return_value=fake):
        with client.websocket_connect("/ws/chat/stream-test") as ws:
            ws.send_json({"type": "message", "text": "hi"})
            events = []
            for _ in range(10):
                try:
                    data = ws.receive_json()
                    events.append(data)
                    if data.get("type") in ("done", "error"):
                        break
                except Exception:
                    break

    token_events = [e for e in events if e["type"] == "token"]
    assert any("Hello world" in e["text"] for e in token_events)


def test_websocket_emits_tool_event(client):
    from langchain_core.messages import ToolMessage
    from skipper import server as srv

    tool_msg = ToolMessage(content="result", name="get_drift_status", tool_call_id="t1")
    fake = _fake_graph()
    fake.stream.return_value = iter([(tool_msg, {})])

    with patch.object(srv, "_get_graph", return_value=fake):
        with client.websocket_connect("/ws/chat/tool-test") as ws:
            ws.send_json({"type": "message", "text": "hi"})
            events = []
            for _ in range(10):
                try:
                    data = ws.receive_json()
                    events.append(data)
                    if data.get("type") in ("done", "error"):
                        break
                except Exception:
                    break

    tool_events = [e for e in events if e["type"] == "tool"]
    assert any(e["name"] == "get_drift_status" for e in tool_events)


def test_api_info_says_whether_long_term_memory_is_actually_on(monkeypatch):
    """The endpoint must distinguish 'configured' from 'active' — that gap is the failure mode."""
    from fastapi.testclient import TestClient
    from skipper import server

    monkeypatch.setattr(
        server, "check_backend", lambda: {"type": "azure", "model": "m", "ok": True}
    )
    monkeypatch.setattr(server, "_get_graph", lambda: type("G", (), {"store": None})())

    body = TestClient(server.app).get("/api/info").json()
    assert body["memory"]["active"] is False
    for field in ("enabled", "backend", "model", "dims", "db", "db_exists"):
        assert field in body["memory"]

    monkeypatch.setattr(server, "_get_graph", lambda: type("G", (), {"store": object()})())
    assert TestClient(server.app).get("/api/info").json()["memory"]["active"] is True
