"""Shared typed transport used by both ``exa ask`` and ``exa chat``."""

from __future__ import annotations

import pytest

from examlops.cli import _agent_transport, _client


def test_build_request_preserves_session_metadata_and_typed_action():
    body = _agent_transport.build_request(
        "approve it",
        "project:session",
        stream=True,
        action=_agent_transport.AgentAction("act.opaque", "approve"),
        metadata={"project": "project"},
    )

    assert body == {
        "model": "examlops-agent",
        "messages": [{"role": "user", "content": "approve it"}],
        "stream": True,
        "user": "project:session",
        "metadata": {"project": "project"},
        "action": {"action_id": "act.opaque", "decision": "approve"},
    }


def test_parse_completion_returns_typed_hitl_result():
    result = _agent_transport.parse_completion(
        {
            "choices": [
                {
                    "message": {"content": "Approval required"},
                    "hitl_required": True,
                    "action_id": "act.pending",
                }
            ]
        }
    ).require_valid()

    assert result.answer == "Approval required"
    assert result.hitl_required is True
    assert result.action_id == "act.pending"


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (None, "invalid completion"),
        ({"choices": []}, "completion with no answer"),
        ({"error": {"message": "backend failed"}}, "backend failed"),
        ({"choices": [{"message": {"content": ""}}]}, "empty answer"),
        (
            {"choices": [{"message": {"content": "approval"}, "hitl_required": True}]},
            "without an action id",
        ),
    ),
)
def test_strict_result_validation_has_one_error_contract(payload, message):
    with pytest.raises(_client.ClientError, match=message):
        _agent_transport.parse_completion(payload).require_valid()


def test_stream_transport_emits_typed_events_and_collects_result(monkeypatch):
    captured = {}

    def fake_sse(url, body, *, token, timeout):
        captured.update(url=url, body=body, token=token, timeout=timeout)
        yield {"ki_event": {"type": "tool_call", "message": "drift_status"}}
        yield {"choices": [{"delta": {"content": "Ready"}}]}
        yield {"ki_event": {"type": "error", "message": "provider warning"}}
        yield {
            "choices": [
                {
                    "delta": {},
                    "hitl_required": True,
                    "action_id": "act.stream",
                }
            ]
        }

    monkeypatch.setattr(_client, "post_sse", fake_sse)
    events = []
    body = _agent_transport.build_request("retrain", "s1", stream=True)
    result = _agent_transport.request_completion(
        "http://agent/",
        "secret",
        body,
        timeout=37.0,
        on_event=events.append,
    )

    assert captured == {
        "url": "http://agent/v1/chat/completions",
        "body": body,
        "token": "secret",
        "timeout": 37.0,
    }
    assert events == [
        _agent_transport.AgentEvent("tool", "drift_status"),
        _agent_transport.AgentEvent("content", "Ready"),
        _agent_transport.AgentEvent("error", "provider warning"),
    ]
    assert result == _agent_transport.AgentResult(
        answer="Ready",
        hitl_required=True,
        action_id="act.stream",
        remote_error="provider warning",
    )


def test_nonstream_transport_uses_same_result_parser(monkeypatch):
    captured = {}

    def fake_post(url, body, *, token, timeout):
        captured.update(url=url, token=token, timeout=timeout)
        return {"choices": [{"message": {"content": "healthy"}}]}

    monkeypatch.setattr(_client, "post", fake_post)
    body = _agent_transport.build_request("status", "s1", stream=False)
    result = _agent_transport.request_completion(
        "http://agent", "secret", body, timeout=12.0
    ).require_valid()

    assert captured == {
        "url": "http://agent/v1/chat/completions",
        "token": "secret",
        "timeout": 12.0,
    }
    assert result.answer == "healthy"


def test_transport_preserves_client_error_status(monkeypatch):
    failure = _client.ClientError("session busy", status=409)

    def reject(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(_client, "post", reject)
    body = _agent_transport.build_request("status", "s1", stream=False)
    with pytest.raises(_client.ClientError) as caught:
        _agent_transport.request_completion("http://agent", "secret", body, timeout=12.0)

    assert caught.value is failure
    assert caught.value.status == 409
