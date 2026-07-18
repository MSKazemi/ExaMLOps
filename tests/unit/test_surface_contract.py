"""Cross-surface contract harness (enterprise-readiness Phase 4, items 4.5/4.8).

The audit's cohesion risk: the SAME logical operation is re-coded in the SDK, the MCP tools, the
agent, and the dashboard — and can silently diverge. This harness boots the surfaces against one
ephemeral data plane and asserts they return **identical** results for shared operations, so a change
that breaks parity fails CI. It also pins the canonical result-envelope contract (item 4.6) that every
agent-callable surface must speak.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture
def active_cluster(tmp_path, monkeypatch):
    """One ephemeral ACTIVE cluster the placement surfaces can both see."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    import examlops.platform_db as pdb
    from examlops.hpc_registry import register_pending

    pdb.init_db()
    register_pending("lxp", "flux", host="lxp-login")
    pdb.set_cluster_state("lxp", "ACTIVE", approved_by="test")
    pdb.record_node_snapshot(
        "lxp", "flux", [{"name": "n0", "gpus": 8, "state": "idle", "cpus": 64}]
    )
    return pdb


def test_placement_sdk_and_mcp_agree(active_cluster):
    """sdk.place and the MCP hpc_place tool must choose the same cluster for the same ask."""
    from examlops import sdk
    from examlops.mcp.tools import hpc_place

    sdk_result = sdk.place(gpus=2, nodes=1)
    mcp_result = hpc_place(gpus=2, nodes=1)

    assert mcp_result["ok"] is True
    assert sdk_result.cluster == mcp_result["cluster"] == "lxp"
    # The MCP tool's envelope is the SDK's PlacementResult serialized — same reason string.
    assert mcp_result["reason"] == sdk_result.reason


def test_every_mcp_tool_speaks_the_result_envelope():
    """Contract (item 4.6): every MCP tool returns a dict with a bool `ok` (+ `error` when False)."""
    import inspect

    from examlops.mcp import tools as t

    for spec in t.REGISTRY:
        sig = inspect.signature(spec.fn)
        # Only exercise zero-required-arg read tools here (write tools need a live control plane).
        if spec.mutating or any(
            p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            for p in sig.parameters.values()
        ):
            continue
        result = spec.fn()
        assert isinstance(result, dict), f"{spec.name} did not return a dict"
        assert "ok" in result and isinstance(result["ok"], bool), f"{spec.name} lacks bool 'ok'"
        if result["ok"] is False:
            assert "error" in result, f"{spec.name} failure envelope lacks 'error'"


def test_result_envelope_shared_shape():
    """The MCP `_err` and sdk.err produce the identical wire shape (single contract)."""
    from examlops.mcp.tools import _err
    from examlops.sdk import err

    assert _err("boom", status=503) == err("boom", status=503).to_dict()
