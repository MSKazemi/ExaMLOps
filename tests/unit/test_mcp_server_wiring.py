"""ADR 0045 cl.1/2 — build_server registers every tool, resource and prompt, and gates writes.

FastMCP is an optional extra, so a recording fake stands in for it; this proves the wiring
(registry -> server), not FastMCP itself.
"""

from __future__ import annotations

from examlops.mcp import server
from examlops.mcp.prompts import iter_prompts
from examlops.mcp.resources import iter_resources
from examlops.mcp.tools import REGISTRY


def _build(monkeypatch, include_writes):
    seen = {"tools": [], "resources": 0, "prompts": []}

    class Fake:
        def __init__(self, name):
            pass

        def tool(self, *, name, description, annotations=None):
            seen["tools"].append(name)
            return lambda fn: fn

        def resource(self, *a, **k):
            seen["resources"] += 1
            return lambda fn: fn

        def prompt(self, *, name, description):
            seen["prompts"].append(name)
            return lambda fn: fn

    monkeypatch.setattr(server, "_import_fastmcp", lambda: Fake)
    server.build_server(include_writes=include_writes)
    return seen


def test_server_registers_resources_and_prompts(monkeypatch):
    seen = _build(monkeypatch, include_writes=True)
    assert seen["resources"] == len(iter_resources()) > 0
    assert sorted(seen["prompts"]) == sorted(p.name for p in iter_prompts())
    assert {"diagnose_drift", "promote_safely", "platform_triage"} <= set(seen["prompts"])


def test_server_omits_write_tools_without_flag(monkeypatch):
    ro = _build(monkeypatch, include_writes=False)["tools"]
    rw = _build(monkeypatch, include_writes=True)["tools"]
    assert len(rw) == len(REGISTRY)
    assert set(ro) < set(rw)
