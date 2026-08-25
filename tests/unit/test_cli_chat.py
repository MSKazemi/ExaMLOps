"""Hermetic tests for the first-party ``exa chat`` terminal client."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli import _client  # noqa: E402
from examlops.cli.commands import agent_cmd  # noqa: E402
from examlops.cli.main import app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
runner = CliRunner()

_INFO = {
    "backend": "ollama",
    "model": "llama3.1:8b",
    "ok": True,
    "memory": {"enabled": True, "active": True},
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("AGENT_URL", "http://agent.test:18004/")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("EXAMLOPS_PROJECT", raising=False)


def _inputs(monkeypatch, *values: str) -> None:
    pending = iter(values)
    monkeypatch.setattr(agent_cmd, "_read_input", lambda _prompt: next(pending))


def _info_and_no_other_get(monkeypatch):
    def fake_get(url, token=""):
        assert url == "http://agent.test:18004/api/info"
        return _INFO

    monkeypatch.setattr(_client, "get", fake_get)


def test_chat_is_first_party_and_has_no_kube_q_dependency():
    pyproject = tomllib.loads((REPO / "platform" / "cli" / "pyproject.toml").read_text())
    extras = pyproject["project"].get("optional-dependencies", {})
    assert "chat" not in extras
    source = (REPO / "platform/cli/src/examlops/cli/commands/agent_cmd.py").read_text()
    assert "subprocess.call" not in source
    assert "shutil.which" not in source


def test_help_is_examlops_native_and_does_not_contact_chat_endpoint(monkeypatch):
    _info_and_no_other_get(monkeypatch)
    _inputs(monkeypatch, "/help", "/quit")
    monkeypatch.setattr(_client, "post", lambda *_a, **_k: pytest.fail("unexpected post"))
    result = runner.invoke(app, ["chat", "--session", "ops-1"])
    assert result.exit_code == 0, result.output
    assert "/resume ID" in result.output
    assert "/approve" in result.output
    assert "Kubernetes" not in result.output
    assert "kube-q" not in result.output


def test_streaming_turns_keep_the_same_server_session(monkeypatch):
    _info_and_no_other_get(monkeypatch)
    _inputs(monkeypatch, "first question", "follow up", "/quit")
    calls = []

    def fake_sse(url, body, token="", timeout=0):
        calls.append((url, body, token, timeout))
        yield {"choices": [{"delta": {"content": "answer"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    monkeypatch.setattr(_client, "post_sse", fake_sse)
    result = runner.invoke(app, ["chat", "--session", "incident-42"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert all(call[0] == "http://agent.test:18004/v1/chat/completions" for call in calls)
    assert [call[1]["user"] for call in calls] == ["incident-42", "incident-42"]
    assert [call[1]["messages"][0]["content"] for call in calls] == [
        "first question",
        "follow up",
    ]


def test_streaming_shows_tools_and_completes_hitl_round_trip(monkeypatch):
    _info_and_no_other_get(monkeypatch)
    _inputs(monkeypatch, "retrain JPCP", "/approve", "/quit")
    sent = []

    def fake_sse(_url, body, **_kwargs):
        sent.append(body["messages"][0]["content"])
        if len(sent) == 1:
            yield {"ki_event": {"type": "tool_call", "message": "trigger_retrain"}}
            yield {
                "choices": [
                    {
                        "delta": {"content": "Approval required"},
                        "hitl_required": True,
                    }
                ]
            }
        else:
            yield {"choices": [{"delta": {"content": "Retrain started"}}]}

    monkeypatch.setattr(_client, "post_sse", fake_sse)
    result = runner.invoke(app, ["chat", "-s", "ops"])

    assert result.exit_code == 0, result.output
    assert sent == ["retrain JPCP", "approve"]
    assert "trigger_retrain" in result.output
    assert "type /approve" in result.output
    assert "Retrain started" in result.output


@pytest.mark.parametrize(("command", "decision"), [("/approve", "approve"), ("/deny", "deny")])
def test_hitl_commands_send_plain_decisions(monkeypatch, command, decision):
    _info_and_no_other_get(monkeypatch)
    _inputs(monkeypatch, command, "/quit")
    sent = []

    def fake_sse(_url, body, **_kwargs):
        sent.append(body["messages"][0]["content"])
        yield {"choices": [{"delta": {"content": "Decision recorded"}}]}

    monkeypatch.setattr(_client, "post_sse", fake_sse)
    result = runner.invoke(app, ["chat", "-s", "approval"])
    assert result.exit_code == 0, result.output
    assert sent == [decision]


def test_no_stream_uses_json_completion_and_never_exposes_token(monkeypatch):
    _info_and_no_other_get(monkeypatch)
    monkeypatch.setenv("AGENT_API_KEY", "private-token")
    _inputs(monkeypatch, "status", "/quit")
    calls = []

    def fake_post(url, body, token="", timeout=0):
        calls.append((url, body, token, timeout))
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "All healthy"},
                    "hitl_required": False,
                }
            ]
        }

    monkeypatch.setattr(_client, "post", fake_post)
    result = runner.invoke(app, ["chat", "--no-stream", "--session", "ops"])
    assert result.exit_code == 0, result.output
    assert calls[0][2] == "private-token"
    assert calls[0][1]["stream"] is False
    assert "All healthy" in result.output
    assert "private-token" not in result.output


def test_sessions_history_new_and_resume_are_server_backed(monkeypatch):
    _inputs(
        monkeypatch,
        "/sessions",
        "/history",
        "/new fresh",
        "/resume prior-2",
        "/history",
        "/quit",
    )
    seen = []

    def fake_get(url, token=""):
        seen.append(url)
        if url.endswith("/api/info"):
            return _INFO
        if url.endswith("/api/threads"):
            return {"threads": ["prior-1", "prior-2"]}
        if url.endswith("/prior-2/history"):
            return {"messages": [{"role": "ai", "content": "remembered answer"}]}
        return {"messages": [{"role": "human", "content": "current question"}]}

    monkeypatch.setattr(_client, "get", fake_get)
    result = runner.invoke(app, ["chat", "-s", "current"])
    assert result.exit_code == 0, result.output
    assert "prior-1" in result.output and "prior-2" in result.output
    assert "current question" in result.output
    assert "remembered answer" in result.output
    assert "Resuming conversation prior-2" in result.output
    assert any("/api/threads/current/history" in url for url in seen)
    assert any("/api/threads/prior-2/history" in url for url in seen)


def test_active_project_namespaces_server_sessions(monkeypatch):
    _info_and_no_other_get(monkeypatch)
    monkeypatch.setenv("EXAMLOPS_PROJECT", "research")
    _inputs(monkeypatch, "status", "/quit")
    bodies = []

    def fake_sse(_url, body, **_kwargs):
        bodies.append(body)
        yield {"choices": [{"delta": {"content": "healthy"}}]}

    monkeypatch.setattr(_client, "post_sse", fake_sse)
    result = runner.invoke(app, ["chat", "--session", "incident-42"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["user"] == "research:incident-42"
    assert bodies[0]["metadata"] == {"project": "research"}
    assert "project research" in result.output


def test_new_resume_and_status_switch_the_server_thread(monkeypatch):
    _inputs(
        monkeypatch,
        "/new fresh",
        "fresh question",
        "/resume prior-2",
        "/status",
        "old follow-up",
        "/quit",
    )
    sent = []

    def fake_get(url, token=""):
        assert url.endswith("/api/info")
        return _INFO

    def fake_sse(_url, body, **_kwargs):
        sent.append((body["user"], body["messages"][0]["content"]))
        yield {"choices": [{"delta": {"content": "ok"}}]}

    monkeypatch.setattr(_client, "get", fake_get)
    monkeypatch.setattr(_client, "post_sse", fake_sse)
    result = runner.invoke(app, ["chat", "-s", "original"])
    assert result.exit_code == 0, result.output
    assert sent == [("fresh", "fresh question"), ("prior-2", "old follow-up")]
    assert "Skipper chat" in result.output
    assert "prior-2" in result.output


def test_failed_turn_keeps_the_repl_open_for_retry(monkeypatch):
    _info_and_no_other_get(monkeypatch)
    _inputs(monkeypatch, "first", "retry", "/quit")
    count = 0

    def fake_sse(*_args, **_kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise _client.ClientError("connection reset")
        yield {"choices": [{"delta": {"content": "worked"}}]}

    monkeypatch.setattr(_client, "post_sse", fake_sse)
    result = runner.invoke(app, ["chat", "-s", "stable"])
    assert result.exit_code == 0, result.output
    assert count == 2
    assert "session is still active" in result.output
    assert "worked" in result.output


def test_unreachable_or_unusable_agent_never_opens_prompt(monkeypatch):
    prompted = []
    monkeypatch.setattr(agent_cmd, "_read_input", lambda prompt: prompted.append(prompt))
    monkeypatch.setattr(
        _client, "get", lambda *_a, **_k: (_ for _ in ()).throw(_client.ClientError("refused"))
    )
    unreachable = runner.invoke(app, ["chat"])
    assert unreachable.exit_code != 0
    assert "make skipper-server" in unreachable.output
    assert not prompted

    monkeypatch.setattr(_client, "get", lambda *_a, **_k: {**_INFO, "ok": False, "fix": "key"})
    unusable = runner.invoke(app, ["chat"])
    assert unusable.exit_code != 0
    assert "not usable" in unusable.output
    assert not prompted


def test_invalid_session_ids_are_rejected_without_network(monkeypatch):
    monkeypatch.setattr(_client, "get", lambda *_a, **_k: pytest.fail("unexpected request"))
    result = runner.invoke(app, ["chat", "--session", "../../private"])
    assert result.exit_code != 0
    assert "Invalid session id" in result.output


def test_json_mode_refuses_instead_of_opening_a_repl(monkeypatch):
    monkeypatch.setattr(_client, "get", lambda *_a, **_k: pytest.fail("unexpected request"))
    result = runner.invoke(app, ["--json", "chat"])
    assert result.exit_code != 0
    assert "interactive" in result.output
