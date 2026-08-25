"""The native chat client must work without package extras or external executables."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from examlops.cli import _client
from examlops.cli.commands import agent_cmd
from examlops.cli.main import app

REPO = Path(__file__).resolve().parents[2]


def test_chat_needs_no_optional_dependency():
    pyproject = tomllib.loads((REPO / "platform/cli/pyproject.toml").read_text())
    extras = pyproject["project"].get("optional-dependencies", {})
    assert "chat" not in extras


def test_chat_does_not_inspect_installed_package_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("AGENT_URL", "http://agent.test:18004")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda _name: (_ for _ in ()).throw(AssertionError("metadata lookup is obsolete")),
    )
    monkeypatch.setattr(
        _client,
        "get",
        lambda _url, token="": {
            "backend": "ollama",
            "model": "llama3.1:8b",
            "ok": True,
            "memory": {"enabled": False, "active": False},
        },
    )
    monkeypatch.setattr(agent_cmd, "_read_input", lambda _prompt: "/quit")

    result = CliRunner().invoke(app, ["chat"])

    assert result.exit_code == 0, result.output
    assert "Skipper" in result.output
