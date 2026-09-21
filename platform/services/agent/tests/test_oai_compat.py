"""Tests for the OpenAI-compatible bridge used by ExaMLOps chat clients."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk, SystemMessage, ToolMessage


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
    intr.id = f"interrupt:{summary}"
    intr.value = {"action": "delete_model", "summary": summary}
    task = MagicMock()
    task.interrupts = [intr]
    return task


@pytest.fixture()
def make_client(monkeypatch):
    """Return a factory that builds a TestClient wired to a given fake graph."""
    from skipper import config, oai_compat
    from skipper import server as srv

    patchers = []
    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", "")
    monkeypatch.setattr(config, "AGENT_TENANT", "default")
    monkeypatch.setattr(oai_compat, "acquire_turn", lambda _thread_id: MagicMock())
    seen_actions: set[str] = set()

    def first_seen(key: str, _ttl_s: float) -> bool:
        if key in seen_actions:
            return False
        seen_actions.add(key)
        return True

    monkeypatch.setattr(oai_compat, "_first_seen_action", first_seen)

    def _factory(graph):
        srv._graph = None
        srv._readonly_graph = None
        srv._backend_info = {}
        cm = patch.object(srv, "_get_graph", return_value=graph)
        readonly_cm = patch.object(srv, "_get_readonly_graph", return_value=graph)
        cm.start()
        readonly_cm.start()
        patchers.extend((cm, readonly_cm))
        client = TestClient(srv.app)
        return client

    yield _factory
    for patcher in reversed(patchers):
        patcher.stop()


def _sse_events(text: str) -> list[dict]:
    """Parse an SSE response body into a list of JSON data objects (skips [DONE])."""
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    return events


def _scoped(session_id: str, *, read_only: bool = False) -> str:
    from skipper.auth import local_identity, scope_thread_id

    return scope_thread_id(local_identity(), session_id, read_only=read_only)


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
    assert call_cfg == {"configurable": {"thread_id": _scoped("my-session")}}


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
    action_id = final[0].get("action_id")
    assert isinstance(action_id, str) and action_id.startswith("act.")
    assert "sess-hitl" not in action_id


def _state_with(*tasks):
    state = MagicMock()
    state.values = {"messages": []}
    state.tasks = list(tasks)
    return state


def test_ordinary_affirmative_text_cannot_resume_pending_write(make_client):
    task = _interrupt_task()
    graph = _fake_graph(tasks=[task])
    client = make_client(graph)

    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "yes"}], "user": "sess-pending"},
    )

    assert response.status_code == 409
    assert "action_id" in response.text
    graph.stream.assert_not_called()


@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.parametrize("decision", ("approve", "deny"))
def test_typed_action_resumes_exact_pending_write_once(make_client, stream, decision):
    from skipper import oai_compat

    task = _interrupt_task()
    intr = task.interrupts[0]
    pending = _state_with(task)
    finished = _state_with()
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="done"), {})])
    graph.get_state.side_effect = [pending, finished]
    client = make_client(graph)
    server_session = _scoped("sess-action")
    action_id = oai_compat._issue_action_id(server_session, intr)
    alternate_action_id = oai_compat._issue_action_id(server_session, intr)
    body = {
        "messages": [{"role": "user", "content": f"decision: {decision}"}],
        "user": "sess-action",
        "stream": stream,
        "action": {"action_id": action_id, "decision": decision},
    }

    response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200
    command = graph.stream.call_args[0][0]
    assert command.resume == decision

    # Even if a stale checkpoint still reports the interrupt, the same token cannot execute twice.
    graph.get_state.side_effect = None
    graph.get_state.return_value = pending
    replay_body = {
        **body,
        "action": {"action_id": alternate_action_id, "decision": decision},
    }
    replay = client.post("/v1/chat/completions", json=replay_body)
    assert replay.status_code == 409
    assert "already used" in replay.text


def test_action_id_is_bound_to_session_and_pending_interrupt(make_client):
    from skipper import oai_compat

    task = _interrupt_task("promote model jpcp")
    graph = _fake_graph(tasks=[task])
    client = make_client(graph)
    action_id = oai_compat._issue_action_id(_scoped("right-session"), task.interrupts[0])

    wrong_session = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "wrong-session"},
        json={
            "messages": [{"role": "user", "content": "approve"}],
            "action": {"action_id": action_id, "decision": "approve"},
        },
    )
    assert wrong_session.status_code == 409
    assert "does not match" in wrong_session.text

    other_task = _interrupt_task("delete model mack")
    graph.get_state.return_value = _state_with(other_task)
    wrong_action = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "right-session"},
        json={
            "messages": [{"role": "user", "content": "approve"}],
            "action": {"action_id": action_id, "decision": "approve"},
        },
    )
    assert wrong_action.status_code == 409
    assert "does not match" in wrong_action.text


def test_expired_action_is_rejected(make_client, monkeypatch):
    from skipper import config, oai_compat

    task = _interrupt_task()
    graph = _fake_graph(tasks=[task])
    client = make_client(graph)
    monkeypatch.setattr(config, "AGENT_ACTION_TTL_SECONDS", 10)
    action_id = oai_compat._issue_action_id(_scoped("sess-expired"), task.interrupts[0], now=100)
    monkeypatch.setattr(oai_compat.time, "time", lambda: 111)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "approve"}],
            "user": "sess-expired",
            "action": {"action_id": action_id, "decision": "approve"},
        },
    )
    assert response.status_code == 409
    assert "expired" in response.text


def test_duplicate_action_is_rejected_when_coordinator_reports_seen(make_client, monkeypatch):
    from skipper import oai_compat

    task = _interrupt_task()
    graph = _fake_graph(tasks=[task])
    client = make_client(graph)
    action_id = oai_compat._issue_action_id(_scoped("sess-duplicate"), task.interrupts[0])
    monkeypatch.setattr(oai_compat, "_first_seen_action", lambda _key, _ttl: False)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "approve"}],
            "user": "sess-duplicate",
            "action": {"action_id": action_id, "decision": "approve"},
        },
    )

    assert response.status_code == 409
    assert "already used" in response.text
    graph.stream.assert_not_called()


def test_coordination_failure_denies_action(make_client, monkeypatch):
    from skipper import oai_compat

    task = _interrupt_task()
    graph = _fake_graph(tasks=[task])
    client = make_client(graph)
    action_id = oai_compat._issue_action_id(_scoped("sess-coord-down"), task.interrupts[0])

    def unavailable(_key, _ttl):
        raise RuntimeError("coordinator unavailable")

    monkeypatch.setattr(oai_compat, "_first_seen_action", unavailable)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "approve"}],
            "user": "sess-coord-down",
            "action": {"action_id": action_id, "decision": "approve"},
        },
    )

    assert response.status_code == 503
    assert "no action was taken" in response.text
    graph.stream.assert_not_called()


@pytest.mark.parametrize(
    ("api_key", "key_map"),
    (("stable-primary", ""), ("", '{"operator":"stable-mapped"}')),
)
def test_action_signing_is_stable_with_configured_credentials(
    make_client, monkeypatch, api_key, key_map
):
    from skipper import config, oai_compat

    monkeypatch.setattr(config, "AGENT_ACTION_SIGNING_KEY", "")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN", "")
    monkeypatch.setattr(config, "AGENT_API_KEY", api_key)
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", key_map)
    first = oai_compat._action_secret()
    monkeypatch.setattr(oai_compat, "_EPHEMERAL_ACTION_SECRET", b"different-process-secret")

    assert oai_compat._action_secret() == first


@pytest.mark.parametrize(
    "action",
    (
        "approve",
        {},
        {"action_id": "id", "decision": "yes"},
        {"action_id": "", "decision": "approve"},
    ),
)
def test_malformed_typed_action_is_rejected(make_client, action):
    client = make_client(_fake_graph())
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "approve"}], "action": action},
    )
    assert response.status_code == 422


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


def test_non_stream_hitl_returns_opaque_action_id(make_client):
    empty = _state_with()
    task = _interrupt_task()
    pending = _state_with(task)
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="review this"), {})])
    graph.get_state.side_effect = [empty, pending]
    client = make_client(graph)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "delete jpcp"}],
            "user": "sess-json-hitl",
            "stream": False,
        },
    )

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["hitl_required"] is True
    assert choice["action_id"].startswith("act.")
    assert "sess-json-hitl" not in choice["action_id"]


def test_read_only_request_uses_isolated_checkpoint_namespace(make_client):
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="safe"), {})])
    client = make_client(graph)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "status"}],
            "user": "dashboard-request",
            "metadata": {"examlops_read_only": True},
        },
    )
    assert response.status_code == 200
    call_cfg = graph.stream.call_args[0][1]
    assert call_cfg == {"configurable": {"thread_id": _scoped("dashboard-request", read_only=True)}}


def test_oai_thread_namespace_comes_from_verified_bearer_not_metadata(make_client, monkeypatch):
    from skipper import config

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", '{"alice":"key-a","bob":"key-b"}')
    monkeypatch.setattr(config, "AGENT_TENANT", "server-tenant")
    graph = _fake_graph()
    graph.stream.side_effect = lambda *_a, **_k: iter([(AIMessageChunk(content="ok"), {})])
    client = make_client(graph)
    body = {
        "messages": [{"role": "user", "content": "status"}],
        "user": "same-client-id",
        "metadata": {"project": "caller-controlled"},
    }

    assert (
        client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": "Bearer key-a"}
        ).status_code
        == 200
    )
    alice_cfg = graph.stream.call_args[0][1]
    assert (
        client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": "Bearer key-b"}
        ).status_code
        == 200
    )
    bob_cfg = graph.stream.call_args[0][1]

    assert alice_cfg != bob_cfg
    assert alice_cfg["configurable"]["thread_id"].endswith(":rw:same-client-id")
    assert "caller-controlled" not in alice_cfg["configurable"]["thread_id"]


def test_other_principal_cannot_resume_pending_action(make_client, monkeypatch):
    from skipper import auth, config, oai_compat

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", '{"alice":"key-a","bob":"key-b"}')
    monkeypatch.setattr(config, "AGENT_TENANT", "tenant")
    task = _interrupt_task()
    pending = _state_with(task)
    empty = _state_with()
    alice = auth.AgentIdentity("alice", "tenant")
    alice_thread = auth.scope_thread_id(alice, "shared-label")
    action_id = oai_compat._issue_action_id(alice_thread, task.interrupts[0])
    graph = _fake_graph()
    graph.get_state.side_effect = lambda cfg: (
        pending if cfg["configurable"]["thread_id"] == alice_thread else empty
    )
    client = make_client(graph)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer key-b"},
        json={
            "messages": [{"role": "user", "content": "approve"}],
            "user": "shared-label",
            "action": {"action_id": action_id, "decision": "approve"},
        },
    )

    assert response.status_code == 409
    assert "no longer pending" in response.text
    graph.stream.assert_not_called()


def test_system_context_reaches_the_graph_instead_of_being_discarded(make_client):
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="grounded"), {})])
    client = make_client(graph)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "UNTRUSTED page context; propose only"},
                {"role": "user", "content": "What should I do?"},
            ],
            "stream": False,
        },
    )
    assert response.status_code == 200
    supplied = graph.stream.call_args[0][0]["messages"]
    assert isinstance(supplied[0], SystemMessage)
    assert "propose only" in supplied[0].content
    assert supplied[-1].content == "What should I do?"


@pytest.mark.parametrize("messages", ([], [{"role": "assistant", "content": "no user"}], ["x"]))
def test_invalid_or_userless_message_arrays_are_rejected(make_client, messages):
    client = make_client(_fake_graph())
    response = client.post("/v1/chat/completions", json={"messages": messages, "stream": False})
    assert response.status_code == 422


@pytest.mark.parametrize("session", ("contains spaces", "../private", "x" * 129, 42))
def test_invalid_session_ids_are_rejected(make_client, session):
    client = make_client(_fake_graph())
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "status"}], "user": session},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error_type", "status", "message"),
    (
        ("busy", 409, "already running"),
        ("unavailable", 503, "coordination is unavailable"),
    ),
)
def test_turn_lock_rejects_overlap_and_coordination_failure(
    make_client, monkeypatch, error_type, status, message
):
    from skipper import oai_compat
    from skipper.turns import TurnBusy, TurnCoordinationUnavailable

    error = TurnBusy("busy") if error_type == "busy" else TurnCoordinationUnavailable("down")

    def reject(_thread_id):
        raise error

    monkeypatch.setattr(oai_compat, "acquire_turn", reject)
    graph = _fake_graph(stream_items=[(AIMessageChunk(content="must not run"), {})])
    client = make_client(graph)
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "status"}],
            "user": "serialized-session",
        },
    )

    assert response.status_code == status
    assert message in response.text
    graph.stream.assert_not_called()


def test_concurrent_http_turns_for_one_session_do_not_overlap(make_client, monkeypatch):
    from skipper import oai_compat
    from skipper.turns import acquire_turn as coordinated_turn

    class Coordinator:
        def __init__(self):
            self.guard = threading.Lock()
            self.holders = {}

        def try_lock(self, key, holder, ttl_s):
            with self.guard:
                current = self.holders.get(key)
                if current not in (None, holder):
                    return False
                self.holders[key] = holder
                return True

        def unlock(self, key, holder):
            with self.guard:
                if self.holders.get(key) == holder:
                    del self.holders[key]

    coordinator = Coordinator()
    entered = threading.Event()
    finish = threading.Event()

    def slow_stream(*_args, **_kwargs):
        entered.set()
        assert finish.wait(timeout=3)
        return iter([(AIMessageChunk(content="first"), {})])

    graph = _fake_graph()
    graph.stream.side_effect = slow_stream
    client = make_client(graph)
    monkeypatch.setattr(
        oai_compat,
        "acquire_turn",
        lambda thread_id: coordinated_turn(thread_id, coordinator=coordinator, ttl_s=30),
    )
    first_response = []

    def first_request():
        first_response.append(
            client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "first"}],
                    "user": "one-session",
                },
            )
        )

    worker = threading.Thread(target=first_request)
    worker.start()
    assert entered.wait(timeout=3)
    second = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "second"}],
            "user": "one-session",
        },
    )
    finish.set()
    worker.join(timeout=3)

    assert second.status_code == 409
    assert len(first_response) == 1 and first_response[0].status_code == 200
    assert graph.stream.call_count == 1


def test_non_stream_response_includes_sanitized_tool_trace(make_client):
    graph = _fake_graph(
        stream_items=[
            (
                ToolMessage(content="sensitive result", name="platform_health", tool_call_id="t1"),
                {},
            ),
            (AIMessageChunk(content="healthy"), {}),
        ]
    )
    client = make_client(graph)
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "status"}], "stream": False},
    )
    trace = response.json()["choices"][0]["trace"]
    assert trace == [{"kind": "tool", "name": "platform_health", "detail": "completed"}]
    assert "sensitive result" not in str(trace)


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


# ── model errors keep the LLM gateway's stable code (ADR 0156) ────────────────


def _gateway_status_error(status: int, error: dict):
    # openai 3.16.2 types its error constructors against its own vendored `httpx2`, not `httpx`
    # (a plain httpx.Response/.Request is not assignable — arg-type).
    import httpx2
    import openai

    response = httpx2.Response(status, request=httpx2.Request("POST", "http://gw/v1/chat"))
    return openai.APIStatusError("gateway error", response=response, body=error)


def _post_failing(make_client, exc):
    graph = _fake_graph()
    graph.stream.side_effect = exc
    client = make_client(graph)
    return client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "q"}], "stream": False},
        headers={"X-Session-ID": "sess-err"},
    )


def test_a_gateway_error_keeps_its_code_and_request_id(make_client):
    exc = _gateway_status_error(
        503,
        {
            "code": "upstream_unavailable",
            "message": "cannot connect to http://10.0.0.5:11434",
            "request_id": "req_abc123",
        },
    )
    resp = _post_failing(make_client, exc)
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["code"] == "upstream_unavailable" and err["request_id"] == "req_abc123"
    assert err["source"] == "llm-gateway"


def test_a_gateway_timeout_status_stays_a_504(make_client):
    resp = _post_failing(
        make_client, _gateway_status_error(504, {"code": "upstream_timeout", "message": "slow"})
    )
    assert resp.status_code == 504 and resp.json()["error"]["code"] == "upstream_timeout"


def test_an_unreachable_gateway_is_reported_as_such(make_client):
    import httpx2
    import openai

    exc = openai.APIConnectionError(request=httpx2.Request("POST", "http://gw/v1/chat"))
    resp = _post_failing(make_client, exc)
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "gateway_unreachable"

    exc = openai.APITimeoutError(request=httpx2.Request("POST", "http://gw/v1/chat"))
    resp = _post_failing(make_client, exc)
    assert resp.status_code == 504 and resp.json()["error"]["code"] == "gateway_timeout"


def test_other_failures_keep_the_historical_500_shape(make_client):
    resp = _post_failing(make_client, RuntimeError("tool exploded"))
    assert resp.status_code == 500
    assert resp.json() == {"error": {"message": "tool exploded"}}
