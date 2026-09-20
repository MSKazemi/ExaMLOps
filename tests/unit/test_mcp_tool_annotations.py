"""Guard: every MCP registry tool carries a consistent safety-annotation set (ADR 0147 d1)."""

from __future__ import annotations

import pytest

from examlops.mcp.agent_card import build_agent_card
from examlops.mcp.tools import REGISTRY, TIERS


@pytest.mark.parametrize("spec", REGISTRY, ids=lambda s: s.name)
def test_annotations_consistent(spec):
    a = spec.annotations
    assert spec.tier in TIERS
    assert set(a) >= {"readOnlyHint", "openWorldHint"}
    assert all(isinstance(v, bool) for v in a.values())
    if spec.mutating:
        assert spec.tier != "read", "a mutating tool must declare a write tier"
        assert a["readOnlyHint"] is False
        assert "destructiveHint" in a and "idempotentHint" in a
    else:
        assert spec.tier == "read"
        assert a["readOnlyHint"] is True
        assert not spec.destructive and not spec.idempotent
        assert "destructiveHint" not in a


def test_human_only_tier_is_destructive():
    for spec in REGISTRY:
        if spec.tier == "C":
            assert spec.annotations["destructiveHint"] is True, spec.name


def test_idempotent_hint_only_on_writes():
    # idempotency is asserted per tool, never defaulted
    idem = [s for s in REGISTRY if s.idempotent]
    assert idem and all(s.mutating for s in idem)


def test_agent_card_carries_annotations():
    card = build_agent_card(include_writes=True)
    assert len(card["skills"]) == len(REGISTRY)
    assert all("annotations" in s for s in card["skills"])


def test_server_passes_annotations(monkeypatch):
    import examlops.mcp.server as server

    seen: dict[str, dict] = {}

    class Fake:
        def __init__(self, name):
            pass

        def tool(self, *, name, description, annotations=None):
            seen[name] = annotations
            return lambda fn: fn

        def resource(self, *a, **k):
            return lambda fn: fn

        def prompt(self, *a, **k):
            return lambda fn: fn

    monkeypatch.setattr(server, "_import_fastmcp", lambda: Fake)
    server.build_server(include_writes=True)
    assert len(seen) == len(REGISTRY)
    assert seen["set_traffic_split"]["destructiveHint"] is True
    assert seen["list_models"]["readOnlyHint"] is True
