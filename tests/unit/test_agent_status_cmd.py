"""Guards for ``exa agent status``.

The command exists because a dead Azure key went unnoticed for a week: the agent answered
every request with an empty string, so it looked like a weak model rather than a rejected
credential. These tests pin the properties that make it worth running — chiefly that it never
reports a *healthy* agent when the answer would be worthless, and that a script can tell the
two failure modes apart from the exit code alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli import _client  # noqa: E402
from examlops.cli.commands import agent_cmd  # noqa: E402

runner = CliRunner()

_HEALTHY = {
    "backend": "azure",
    "model": "gpt-5.5",
    "ok": True,
    "memory": {"enabled": True, "active": True},
}


@pytest.fixture(autouse=True)
def _quiet_config(monkeypatch):
    """Pin the endpoint so the tests do not depend on the developer's config file."""
    monkeypatch.setenv("AGENT_URL", "http://agent.test:18004")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)


def _reply(monkeypatch, payload):
    monkeypatch.setattr(_client, "get", lambda url, token="": payload)


def _fail(monkeypatch, exc):
    def boom(url, token=""):
        raise exc

    monkeypatch.setattr(_client, "get", boom)


def test_healthy_agent_exits_zero_and_names_the_model(monkeypatch):
    _reply(monkeypatch, _HEALTHY)
    result = runner.invoke(agent_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "gpt-5.5" in result.output
    assert "azure" in result.output


def test_unreachable_agent_is_not_reported_as_healthy(monkeypatch):
    _fail(monkeypatch, _client.ClientError("connection refused"))
    result = runner.invoke(agent_cmd.app, ["status"])
    assert result.exit_code != 0
    assert "Could not reach" in result.output


def test_a_running_agent_with_a_dead_backend_still_fails(monkeypatch):
    """The exact outage this command was written for.

    The agent is up and will happily answer; the answers are worthless. Reporting this as
    healthy — because the HTTP call succeeded — is the failure mode to prevent.
    """
    _reply(
        monkeypatch,
        {
            "backend": "azure",
            "model": "gpt-5.5",
            "ok": False,
            "memory": {"enabled": False, "active": False},
            "fix": "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT",
        },
    )
    result = runner.invoke(agent_cmd.app, ["status"])
    assert result.exit_code != 0
    assert "not usable" in result.output
    assert "AZURE_OPENAI_API_KEY" in result.output, "the fix hint must reach the operator"


def test_memory_enabled_but_not_attached_is_surfaced_as_a_warning(monkeypatch):
    """Silent degradation: the agent works, answers are just quietly worse."""
    _reply(
        monkeypatch,
        {**_HEALTHY, "memory": {"enabled": True, "active": False}},
    )
    result = runner.invoke(agent_cmd.app, ["status"])
    assert result.exit_code == 0, "a memory gap must not fail the health gate"
    assert "NOT attached" in result.output
    assert "AGENT_EMBED_BACKEND" in result.output


def test_memory_off_is_a_choice_not_a_warning(monkeypatch):
    _reply(monkeypatch, {**_HEALTHY, "memory": {"enabled": False, "active": False}})
    result = runner.invoke(agent_cmd.app, ["status"])
    assert result.exit_code == 0
    assert "short-term only" in result.output
    assert "NOT attached" not in result.output


def test_json_is_one_object_and_still_exits_nonzero_when_unusable(monkeypatch):
    """A CI gate parses the body *and* reads the exit code; both must agree."""
    import json

    from examlops.cli import _output

    monkeypatch.setattr(_output, "json_mode", True)
    _reply(monkeypatch, {**_HEALTHY, "ok": False, "fix": "ANTHROPIC_API_KEY"})
    result = runner.invoke(agent_cmd.app, ["status"])

    assert result.exit_code != 0
    body = json.loads(result.stdout)
    assert body["reachable"] is True
    assert body["backend_ok"] is False
    assert body["fix"] == "ANTHROPIC_API_KEY"


def test_a_non_object_response_is_not_mistaken_for_an_agent(monkeypatch):
    """Something else answering on the port — a proxy, a login page — is not an agent."""
    _reply(monkeypatch, "<html>Sign in</html>")
    result = runner.invoke(agent_cmd.app, ["status"])
    assert result.exit_code != 0
    assert "really the agent" in result.output
