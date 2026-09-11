"""ADR 0130 — MCP surface for the dataplane."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.data import init_db  # noqa: E402
from examlops.mcp import tools  # noqa: E402


def test_read_tools_never_raise():
    init_db()
    assert tools.dataplane_sources()["sources"] == []
    out = tools.dataplane_snapshots("nope")
    assert "snapshots" in out or "error" in out


def test_pull_of_a_missing_source_returns_an_error(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MCP_ALLOW_WRITES", raising=False)
    assert (
        "error" in tools.dataplane_pull("nope") or tools.dataplane_pull("nope").get("ok") is False
    )


def test_registry_declares_them():
    names = {spec.fn.__name__: spec for spec in tools.REGISTRY}
    assert names["dataplane_sources"].mutating is False
    assert names["dataplane_pull"].mutating is True and names["dataplane_pull"].tier in {
        "A",
        "B",
        "C",
    }
