"""Tests for the OpenAI-compatible chat bridge consumed by kube-q (`kq`)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk, ToolMessage


def _fake_graph(stream_items=None, tasks=None):
    """A MagicMock graph with a configurable stream and interrupt state."""
    state_mock = MagicMock()
    state_mock.values = {"messages": []}
    state_mock.tasks = tasks if tasks is not None else []
    graph = MagicMock()
    graph.get_state.return_value = state_mock
    graph.stream.return_value = iter(stream_items or [])
    graph.checkpointer.list.return_value = []
    return graph


def _interrupt_task(summary="delete model jpcp"):
    intr = MagicMock()
    intr.value = {"action": "delete_model", "summary": summary}
    task = MagicMock()
    task.interrupts = [intr]
    return task


@pytest.fixture()
def make_client():
    """Return a factory that builds a TestClient wired to a given fake graph."""
    from skipper import server as srv

    def _factory(graph):
        srv._graph = None
        srv._backend_info = {}
        cm = patch.object(srv, "_get_graph", return_value=graph)
        cm.start()
        client = TestClient(srv.app)
        client._patch_cm = cm  # keep a handle so we can stop it
        return client

    yield _factory


def _sse_events(text: str) -> list[dict]:
    """Parse an SSE response body into a list of JSON data objects (skips [DONE])."""
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    return events


# ── /healthz ───────────────────────────────────────────────────────────────────


def test_healthz(make_client):
    client = make_client(_fake_graph())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── streaming ──────────────────────────────────────────────────────────────────


def test_stream_emits_token_and_stop(make_client):
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="Hello world"), {})])
    client = make_client(graph)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"X-Session-ID": "sess-1"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = _sse_events(resp.text)
    assert "[DONE]" in resp.text

    contents = [
        c["delta"].get("content")
        for e in events
        for c in e.get("choices", [])
        if c.get("delta", {}).get("content")
    ]
    assert any("Hello world" in c for c in contents)

    finishes = [c.get("finish_reason") for e in events for c in e.get("choices", [])]
    assert "stop" in finishes


def test_stream_routes_session_to_thread(make_client):
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="ok"), {})])
    client = make_client(graph)
    client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"X-Session-ID": "my-session"},
    )
    # graph.stream was invoked with the thread_id derived from X-Session-ID.
    call_cfg = graph.stream.call_args[0][1]
    assert call_cfg == {"configurable": {"thread_id": "my-session"}}


def test_stream_surfaces_tool_call(make_client):
    tool_msg = ToolMessage(content="r", name="get_drift_status", tool_call_id="t1")
    graph = _fake_graph(stream_items=[(tool_msg, {})])
    client = make_client(graph)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"X-Session-ID": "sess-tool"},
    )
    events = _sse_events(resp.text)
    ki = [e["ki_event"] for e in events if "ki_event" in e]
    assert any(k.get("tool_name") == "get_drift_status" for k in ki)


def test_stream_hitl_final_chunk(make_client):
    # No pending interrupt at input time, one pending after the turn.
    empty_state = MagicMock()
    empty_state.tasks = []
    hitl_state = MagicMock()
    hitl_state.tasks = [_interrupt_task("delete model jpcp")]
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="working"), {})])
    graph.get_state.side_effect = [empty_state, hitl_state, hitl_state]

    client = make_client(graph)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "delete jpcp"}],
            "stream": True,
        },
        headers={"X-Session-ID": "sess-hitl"},
    )
    events = _sse_events(resp.text)
    final = [c for e in events for c in e.get("choices", []) if c.get("finish_reason") == "stop"]
    assert final and final[0].get("hitl_required") is True
    assert final[0].get("action_id") == "sess-hitl"


# ── non-streaming ──────────────────────────────────────────────────────────────


def test_non_stream_returns_message(make_client):
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="Answer text"), {})])
    client = make_client(graph)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "q"}], "stream": False},
        headers={"X-Session-ID": "sess-ns"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Answer text"
    assert data["choices"][0]["hitl_required"] is False


# ── auth gate ──────────────────────────────────────────────────────────────────


def test_auth_gate_rejects_without_key(make_client):
    from skipper import config

    graph = _fake_graph(stream_items=[(AIMessageChunk(content="x"), {})])
    client = make_client(graph)
    with patch.object(config, "AGENT_API_KEY", "secret"):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        )
    assert resp.status_code == 401


def test_auth_gate_accepts_with_key(make_client):
    from skipper import config

    graph = _fake_graph(stream_items=[(AIMessageChunk(content="ok"), {})])
    client = make_client(graph)
    with patch.object(config, "AGENT_API_KEY", "secret"):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            headers={"Authorization": "Bearer secret"},
        )
    assert resp.status_code == 200


# ── streaming must reach into sub-agents ─────────────────────────────────────


def test_stream_messages_asks_for_subgraph_messages():
    """The specialists are subgraphs, so a top-level-only stream carries no assistant tokens.

    Measured 2026-08-23 against the live graph: 0 ``AIMessageChunk``s without ``subgraphs=True``
    and 503 with it, which is why every bridge answer came back empty while the finished reply sat
    in the checkpoint.
    """
    from skipper.server import stream_messages

    graph = _fake_graph()
    list(stream_messages(graph, {"messages": []}, {"configurable": {"thread_id": "t"}}))
    assert graph.stream.call_args.kwargs["subgraphs"] is True
    assert graph.stream.call_args.kwargs["stream_mode"] == "messages"


def test_stream_messages_unwraps_both_item_shapes():
    from skipper.server import stream_messages

    flat = (AIMessageChunk(content="top"), {})
    nested = (("specialist:1",), (AIMessageChunk(content="inner"), {}))
    graph = _fake_graph(stream_items=[flat, nested])
    out = list(stream_messages(graph, {}, {}))
    assert [m.content for m, _meta in out] == ["top", "inner"]


def test_collector_falls_back_to_the_answer_in_state():
    """A turn that streams no text must not report an empty answer when the state has one."""
    from skipper import oai_compat

    graph = _fake_graph(stream_items=[(ToolMessage(content="{}", tool_call_id="c1", name="t"), {})])
    graph.get_state.return_value.values = {"messages": [AIMessageChunk(content="run `exa status`")]}
    answer, _tools, _usage, _intr = oai_compat._run_graph_collect(
        graph,
        {"configurable": {"thread_id": "sess-fallback"}},
        {"messages": []},
        lambda c: c if isinstance(c, str) else "",
    )
    assert "exa status" in answer
