"""CLI contract for remote-by-default agent memory governance."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli import _client, _output  # noqa: E402
from examlops.cli.commands import agent_cmd  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("AGENT_URL", "http://agent.test:18004")
    monkeypatch.setenv("AGENT_API_KEY", "configured-token")
    monkeypatch.setattr(_output, "json_mode", False)
    monkeypatch.setattr(_output, "yes_mode", False)


def test_stats_uses_authenticated_remote_endpoint_by_default(monkeypatch):
    calls = []

    def fake_get(url, token=""):
        calls.append((url, token))
        return {"counts": {"proc": 1, "episode": 0, "pref": 0, "kb": 0}}

    monkeypatch.setattr(_client, "get", fake_get)
    monkeypatch.setattr(
        agent_cmd, "_local_store", lambda: pytest.fail("local store opened without --local")
    )
    result = runner.invoke(agent_cmd.app, ["memory", "stats"])

    assert result.exit_code == 0, result.output
    assert calls == [("http://agent.test:18004/api/memory/stats", "configured-token")]


def test_delete_requires_confirmation_before_remote_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        _client,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"erased": 1},
    )

    declined = runner.invoke(agent_cmd.app, ["memory", "delete", "kb"], input="n\n")
    assert declined.exit_code == 0, declined.output
    assert calls == []

    approved = runner.invoke(agent_cmd.app, ["memory", "delete", "kb"], input="y\n")
    assert approved.exit_code == 0, approved.output
    body = calls[0][0][1]
    assert body == {"kind": "kb", "scope": None, "confirmation": "erase-owned-memory"}
    assert calls[0][1]["token"] == "configured-token"


def test_list_export_and_review_use_remote_owner_endpoints(monkeypatch):
    gets = []
    posts = []

    def fake_get(url, token=""):
        gets.append((url, token))
        if url.endswith("/api/memory/export"):
            return {"memories": {kind: [] for kind in agent_cmd._MEMORY_KINDS}}
        if url.endswith("/api/memory/reviews"):
            return {"reviews": []}
        return {"items": []}

    def fake_post(url, body, token="", timeout=0):
        posts.append((url, body, token, timeout))
        return {"review_id": 7, "status": "approved"}

    monkeypatch.setattr(_client, "get", fake_get)
    monkeypatch.setattr(_client, "post", fake_post)

    assert runner.invoke(agent_cmd.app, ["memory", "list", "proc"]).exit_code == 0
    assert runner.invoke(agent_cmd.app, ["memory", "export"]).exit_code == 0
    assert runner.invoke(agent_cmd.app, ["memory", "review", "list"]).exit_code == 0
    assert runner.invoke(agent_cmd.app, ["memory", "review", "approve", "7"]).exit_code == 0
    assert all(token == "configured-token" for _, token in gets)
    assert [url.rsplit("/api", 1)[1] for url, _ in gets] == [
        "/memory/list/proc?limit=50",
        "/memory/export",
        "/memory/reviews",
    ]
    assert posts == [
        ("http://agent.test:18004/api/memory/reviews/7/approve", {}, "configured-token", 30.0)
    ]


def test_json_delete_still_requires_explicit_yes(monkeypatch):
    monkeypatch.setattr(_output, "json_mode", True)
    monkeypatch.setattr(
        _client, "post", lambda *_args, **_kwargs: pytest.fail("delete must not be sent")
    )

    result = runner.invoke(agent_cmd.app, ["memory", "delete", "pref"])

    assert result.exit_code == 1
    assert "explicit consent" in result.output


def test_export_file_is_owner_only(monkeypatch, tmp_path):
    monkeypatch.setattr(
        _client,
        "get",
        lambda *_args, **_kwargs: {"memories": {kind: [] for kind in agent_cmd._MEMORY_KINDS}},
    )
    destination = tmp_path / "memory-export.json"

    result = runner.invoke(agent_cmd.app, ["memory", "export", "--out", str(destination)])

    assert result.exit_code == 0, result.output
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
def test_export_refuses_to_follow_a_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(
        _client,
        "get",
        lambda *_args, **_kwargs: {"memories": {kind: [] for kind in agent_cmd._MEMORY_KINDS}},
    )
    target = tmp_path / "private-target.json"
    target.write_text("keep", encoding="utf-8")
    destination = tmp_path / "memory-export.json"
    destination.symlink_to(target)

    result = runner.invoke(agent_cmd.app, ["memory", "export", "--out", str(destination)])

    assert result.exit_code != 0
    assert target.read_text(encoding="utf-8") == "keep"


def test_local_mode_is_explicit(monkeypatch):
    class Store:
        pass

    monkeypatch.setattr(agent_cmd, "_local_store", lambda: (object(), Store()))
    monkeypatch.setattr(
        _client,
        "get",
        lambda *_args, **_kwargs: pytest.fail("remote endpoint called in local mode"),
    )
    import types

    package = types.ModuleType("skipper")
    fake = types.ModuleType("skipper.memory_types")
    fake.stats = lambda _store: {"proc": 0, "episode": 0, "pref": 0, "kb": 0}
    fake_config = types.SimpleNamespace(AGENT_MEMORY_DB="/tmp/test-memory.db")
    package.memory_types = fake
    package.config = fake_config
    monkeypatch.setitem(sys.modules, "skipper", package)
    monkeypatch.setitem(sys.modules, "skipper.memory_types", fake)

    result = runner.invoke(agent_cmd.app, ["memory", "stats", "--local"])

    assert result.exit_code == 0, result.output
