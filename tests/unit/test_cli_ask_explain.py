from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner  # noqa: E402

from examlops.cli import _client  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


# ── exa ask ───────────────────────────────────────────────────────────────────


def _fake_completion(content: str, hitl: bool = False, action_id: str | None = None) -> dict:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
                "hitl_required": hitl,
                "action_id": action_id,
            }
        ]
    }


def test_ask_prints_agent_answer(monkeypatch):
    captured = {}

    def fake_post(url, body, token="", timeout=10.0):
        captured["url"] = url
        captured["body"] = body
        return _fake_completion("JPCP and MACK are in production.")

    monkeypatch.setattr(_client, "post", fake_post)
    result = runner.invoke(app, ["ask", "which", "models", "are", "in", "production?"])
    assert result.exit_code == 0, result.output
    assert "JPCP and MACK are in production." in result.output
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["messages"][0]["content"] == "which models are in production?"


def test_ask_json_mode(monkeypatch):
    monkeypatch.setattr(_client, "post", lambda *a, **k: _fake_completion("hi"))
    result = runner.invoke(app, ["--json", "ask", "hello"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["answer"] == "hi"
    assert payload["hitl_required"] is False


def test_ask_hitl_hint(monkeypatch):
    monkeypatch.setattr(
        _client,
        "post",
        lambda *a, **k: _fake_completion("Ready.", hitl=True, action_id="act.opaque"),
    )
    result = runner.invoke(app, ["ask", "retrain jpcp", "--session", "s1"])
    assert result.exit_code == 0, result.output
    assert "needs approval" in result.output.lower()
    assert "--approve act.opaque" in result.output


def test_ask_sends_typed_approval(monkeypatch):
    captured = {}

    def fake_post(url, body, token="", timeout=10.0):
        captured["body"] = body
        return _fake_completion("Approved.")

    monkeypatch.setattr(_client, "post", fake_post)
    result = runner.invoke(
        app, ["ask", "--no-stream", "--session", "s1", "--approve", "act.opaque"]
    )
    assert result.exit_code == 0, result.output
    assert captured["body"]["action"] == {"action_id": "act.opaque", "decision": "approve"}


def test_ask_typed_decision_requires_session(monkeypatch):
    monkeypatch.setattr(_client, "post", lambda *a, **k: None)
    result = runner.invoke(app, ["ask", "--approve", "act.opaque"])
    assert result.exit_code == 1
    assert "requires --session" in result.output


def test_ask_agent_unreachable(monkeypatch):
    def boom(*a, **k):
        raise _client.ClientError("connection refused")

    monkeypatch.setattr(_client, "post", boom)
    result = runner.invoke(app, ["ask", "hello"])
    assert result.exit_code == 1
    assert "Skipper agent" in result.output


def test_ask_empty_question():
    result = runner.invoke(app, ["ask", "   "])
    assert result.exit_code == 1
    assert "Empty question" in result.output


# ── exa explain ─────────────────────────────────────────────────────────────


def test_explain_lists_top_level_commands():
    result = runner.invoke(app, ["explain"])
    assert result.exit_code == 0, result.output
    assert "drift" in result.output
    assert "mcp" in result.output


def test_explain_group_lists_subcommands():
    result = runner.invoke(app, ["explain", "mcp"])
    assert result.exit_code == 0, result.output
    assert "serve" in result.output
    assert "agent-card" in result.output


def test_explain_leaf_shows_examples():
    result = runner.invoke(app, ["explain", "serve", "reload"])
    assert result.exit_code == 0, result.output
    assert "exa serve reload" in result.output


def test_explain_unknown_command_errors():
    result = runner.invoke(app, ["explain", "definitely-not-a-command"])
    assert result.exit_code == 1
    assert "Unknown command" in result.output


def test_explain_json_mode():
    result = runner.invoke(app, ["--json", "explain", "serve", "reload"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "exa serve reload"
    assert any("exa serve reload" in ex for ex in payload["examples"])


# ── did-you-mean suggestions ──────────────────────────────────────────────────


def test_unknown_command_suggests_close_match():
    result = runner.invoke(app, ["modls"])
    assert result.exit_code == 2
    # Either Click's native suggestion or our fallback names the real command.
    assert "models" in result.output.lower()
