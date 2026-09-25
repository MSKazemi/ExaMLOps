"""ADR 0078 clause 1 — ``examlops.drift`` / ``examlops.audit`` / ``examlops.hpc`` read tranche."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import examlops
from examlops.sdk import audit, drift, hpc
from examlops.sdk.errors import InvalidArgumentError, UnavailableError

_PKG = Path(examlops.__file__).parent


@pytest.mark.parametrize("name", ["models", "drift", "audit", "hpc"])
def test_sdk_namespace_names_are_reserved(name):
    """`examlops.<name>` is aliased to the SDK namespace; a real module there would be shadowed."""
    assert not (_PKG / f"{name}.py").exists() and not (_PKG / name).is_dir(), (
        f"examlops/{name} would be shadowed by the SDK namespace alias — pick another name"
    )
    assert getattr(examlops, name).__name__ == f"examlops.sdk.{name}"
    assert name in examlops.__all__


# ── drift ───────────────────────────────────────────────────────────────────────────────────────
def test_drift_status_is_typed_and_matches_the_shared_computation(monkeypatch):
    from examlops import drift_status

    row = {
        "model": "JPCP",
        "live_mean": 1.5,
        "live_std": 0.2,
        "baseline_mean": None,
        "z_score": 3.4,
        "status": "CRITICAL",
        "n_snapshots": 100,
        "recent": [1.0, 2.0],
    }
    seen: list = []
    monkeypatch.setattr(drift_status, "model_rows", lambda m=None: seen.append(m) or [row])
    [st] = drift.status("JPCP")
    assert seen == ["JPCP"]
    assert isinstance(st, drift.DriftStatus) and st.status == "CRITICAL"
    assert st.baseline_mean is None, "no baseline is not a baseline of 0"
    assert st.to_dict() == row and list(st.to_dict()) == list(row)


def test_drift_status_over_real_snapshots():
    from examlops.data import get_db, init_db

    init_db()
    with get_db() as conn:
        for i in range(12):
            conn.execute(
                "INSERT INTO drift_snapshots (model, alias, prediction) VALUES (?, ?, ?)",
                ("JPCP", "Production", float(i)),
            )
    [st] = drift.status()
    assert st.model == "JPCP" and st.n_snapshots == 12
    assert drift.status("OTHER") == []


def test_drift_failure_is_unavailable(monkeypatch):
    from examlops import drift_status

    def boom(m=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(drift_status, "model_rows", boom)
    with pytest.raises(UnavailableError, match="database is locked"):
        drift.status()


# ── audit ───────────────────────────────────────────────────────────────────────────────────────
def _write(n: int, tenant: str, action: str = "a") -> None:
    from examlops.data.audit import write_audit_event

    for i in range(n):
        write_audit_event("sdk-test", "alice", action, f"M{i}", {"i": i}, tenant=tenant)


def test_audit_tenant_filter_applies_before_the_limit():
    """Twenty newer rows of another tenant must not push this tenant's rows out of the window."""
    _write(3, "acme")
    _write(20, "other")
    rows = audit.query(tenant="acme", limit=5)
    assert len(rows) == 3 and {r.tenant for r in rows} == {"acme"}
    assert [r.details["i"] for r in rows] == [2, 1, 0], "newest first, in the chain's own order"


def test_audit_filters_and_decodes_details():
    _write(2, "default", action="model_approved")
    _write(1, "default", action="other")
    rows = audit.query(action="model_approved")
    assert {r.action for r in rows} == {"model_approved"} and len(rows) == 2
    assert isinstance(rows[0].details, dict)
    assert audit.query(model="M0", action="other")[0].target == "M0"
    json.dumps([r.to_dict() for r in rows])  # serialisable as-is


def test_audit_limit_is_bounded_and_validated():
    _write(3, "default")
    assert len(audit.query(limit=10**9)) == 3  # capped at MAX_LIMIT, not an error
    with pytest.raises(InvalidArgumentError):
        audit.query(limit=0)
    with pytest.raises(InvalidArgumentError):
        audit.query(since_days=-1)


def test_audit_limit_cap_really_bounds_the_rows(monkeypatch):
    """The cap is observed in the rows returned, not only in the absence of an error."""
    from examlops.sdk import audit as sdk_audit

    _write(3, "default")
    monkeypatch.setattr(sdk_audit, "MAX_LIMIT", 2)
    rows = audit.query(limit=10**9)
    assert [r.details["i"] for r in rows] == [2, 1]


def test_cli_audit_renders_the_sdk_query():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _write(2, "default", action="sdk_rendered")
    result = CliRunner().invoke(app, ["--json", "audit", "--action", "sdk_rendered"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert [r["action"] for r in doc] == ["sdk_rendered", "sdk_rendered"]
    assert doc[0]["details"] == {"i": 1}
    assert set(doc[0]) == {"id", "ts", "source", "actor", "action", "target", "details"}


# ── hpc ─────────────────────────────────────────────────────────────────────────────────────────
def test_hpc_clusters_are_typed(monkeypatch):
    from examlops import hpc_registry

    raw = {
        "name": "lxp",
        "scheduler": "flux",
        "transport": "ssh",
        "host": "h",
        "state": "ACTIVE",
        "approved_by": "root",
        "requested_by": "bob",
        "reason": None,
        "capabilities": {"total_gpus": 4},
    }
    monkeypatch.setattr(hpc_registry, "list_clusters", lambda: [raw])
    [c] = hpc.clusters()
    assert isinstance(c, hpc.Cluster) and c.state == "ACTIVE" and c.scheduler == "flux"
    assert c.to_dict() == raw


def test_hpc_capacity_is_typed(monkeypatch):
    from examlops import hpc_registry

    monkeypatch.setattr(
        hpc_registry,
        "active_clusters_with_inventory",
        lambda: [{"name": "lxp", "scheduler": "flux", "capabilities": {"total_gpus": 8}}],
    )
    [cap] = hpc.capacity()
    assert isinstance(cap, hpc.ClusterCapacity)
    assert cap.name == "lxp" and cap.total_gpus == 8 and cap.gpu_hours_used == 0.0


def test_hpc_registry_failure_is_unavailable(monkeypatch):
    from examlops import hpc_registry

    def boom():
        raise OSError("clusters.yaml unreadable")

    monkeypatch.setattr(hpc_registry, "list_clusters", boom)
    with pytest.raises(UnavailableError):
        hpc.clusters()


def test_mcp_hpc_clusters_tool_reads_through_the_sdk(monkeypatch):
    from examlops.mcp import tools

    monkeypatch.setattr(hpc, "clusters", lambda: [hpc.Cluster("x", "PENDING", raw={"name": "x"})])
    assert tools.hpc_clusters() == {"ok": True, "clusters": [{"name": "x"}]}
