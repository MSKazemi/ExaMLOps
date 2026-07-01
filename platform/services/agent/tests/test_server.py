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
def client():
    from skipper import server as srv

    # Reset shared graph state so each test starts clean
    srv._graph = None
    srv._backend_info = {}
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

    fake = _fake_graph()
    t1 = MagicMock()
    t1.config = {"configurable": {"thread_id": "cli-aaa"}}
    t2 = MagicMock()
    t2.config = {"configurable": {"thread_id": "cli-bbb"}}
    fake.checkpointer.list.return_value = [t1, t2]
    with patch.object(srv, "_get_graph", return_value=fake):
        resp = client.get("/api/threads")
    assert set(resp.json()["threads"]) == {"cli-aaa", "cli-bbb"}


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
