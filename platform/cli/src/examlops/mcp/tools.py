"""Agent-callable tools for ExaMLOps — pure, FastMCP-free.

Every tool is a plain typed function returning JSON-serialisable ``dict``s (never raising
for expected failures — it returns a structured ``{"error": ...}`` envelope instead, so an
LLM agent can reason about and recover from failures). The functions reuse the same
``load_config()`` + ``_client`` + ``platform_db`` code paths as the ``exa`` CLI, so the
agent surface and the human surface can never drift apart.

The :data:`REGISTRY` lists each tool together with a ``mutating`` flag. Read-only tools are
always exposed; mutating tools are only registered on the MCP server when writes are
explicitly enabled (see :mod:`examlops.mcp.server`).

This module has **no third-party dependencies** and is fully unit-testable without FastMCP.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from examlops.cli import _client
from examlops.cli._config import Config, load_config

# ── envelope helpers ──────────────────────────────────────────────────────────


def _err(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _cfg() -> Config:
    return load_config()


def _get(url: str, token: str = "") -> dict[str, Any]:
    """GET returning either the parsed body or a structured error envelope."""
    try:
        data = _client.get(url, token=token)
    except _client.ClientError as exc:
        return _err(str(exc), status=getattr(exc, "status", None))
    return {"ok": True, "data": data}


# ── read-only tools ─────────────────────────────────────────────────────────


def platform_status() -> dict[str, Any]:
    """Return a health snapshot of the ExaMLOps platform.

    Reports which core services (control plane, MLflow, Prefect, Ray Serve, dashboard)
    are reachable, loaded serving models, active pipeline runs and pending approvals.
    Use this first to understand overall platform state.
    """
    cfg = _cfg()
    res = _get(f"{cfg.control_plane_url}/status", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **res["data"]}


def list_models() -> dict[str, Any]:
    """List every registered model with its Production alias, latest version and aliases.

    Reads the MLflow model registry. Model names are lowercase in the registry
    (e.g. ``jpcp``).
    """
    cfg = _cfg()
    res = _get(f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/search")
    if not res.get("ok"):
        return res
    models = res["data"].get("registered_models", [])
    out = []
    for m in models:
        aliases = {a["alias"]: a["version"] for a in m.get("aliases", [])}
        latest = max((v["version"] for v in m.get("latest_versions", [])), default=None)
        out.append(
            {
                "name": m["name"],
                "production": aliases.get("Production"),
                "latest": latest,
                "aliases": aliases,
            }
        )
    return {"ok": True, "models": out, "count": len(out)}


def model_detail(name: str) -> dict[str, Any]:
    """Return full detail for one registered model: versions, aliases and metrics.

    Args:
        name: Registered model name, lowercase (e.g. ``jpcp``).
    """
    cfg = _cfg()
    q = urllib.parse.quote(name)
    res = _get(f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get?name={q}")
    if not res.get("ok"):
        return res
    return {"ok": True, "model": res["data"].get("registered_model", {})}


def list_production_models() -> dict[str, Any]:
    """List the models the control plane knows about and their associated datasets."""
    cfg = _cfg()
    res = _get(f"{cfg.control_plane_url}/models", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


def list_approvals() -> dict[str, Any]:
    """List pending sysadmin approval requests in the promotion gate."""
    cfg = _cfg()
    res = _get(f"{cfg.control_plane_url}/approvals", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


def modelzoo_status() -> dict[str, Any]:
    """Report ModelZoo repository freshness and the latest upstream events."""
    cfg = _cfg()
    res = _get(f"{cfg.control_plane_url}/modelzoo/status", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


def recent_audit_events(limit: int = 20, model: str | None = None) -> dict[str, Any]:
    """Return recent platform audit events (mutations, retrains, promotions, drift).

    Args:
        limit: Maximum number of events to return (most recent first). Default 20.
        model: Optional case-insensitive filter on the event target (model name).
    """
    try:
        from examlops.platform_db import get_db, init_db

        init_db()
        limit = max(1, min(int(limit), 500))
        sql = "SELECT ts, source, actor, action, target, details FROM audit_events"
        params: list[Any] = []
        if model:
            sql += " WHERE target LIKE ?"
            params.append(f"%{model}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"audit query failed: {exc}")
    events = [
        {
            "ts": r[0],
            "source": r[1],
            "actor": r[2],
            "action": r[3],
            "target": r[4],
            "details": r[5],
        }
        for r in rows
    ]
    return {"ok": True, "events": events, "count": len(events)}


def _as_dict(data: Any) -> dict[str, Any]:
    """Normalise a list-or-dict API body into a dict envelope for tool output."""
    if isinstance(data, dict):
        return data
    return {"items": data}


# ── mutating tools (gated behind EXAMLOPS_MCP_ALLOW_WRITES) ────────────────────


def trigger_retrain(
    model_name: str,
    dataset_name: str,
    dummy: bool = False,
    backend_name: str | None = None,
) -> dict[str, Any]:
    """Trigger a training/retraining run for a model via the control plane.

    This is a mutating action — it launches a Prefect flow run. Requires
    CONTROL_PLANE_TOKEN to be configured. Returns the flow run id and a status URL
    the agent can poll with the retrain_status tool.

    Args:
        model_name: Registered model name (e.g. ``JPCP``).
        dataset_name: Dataset class name (e.g. ``PM100Dataset``).
        dummy: If true, run a fast dummy training loop instead of full training.
        backend_name: Optional dataset backend override (zenodo/minio/dataplane).
    """
    cfg = _cfg()
    if not cfg.control_plane_token:
        return _err("CONTROL_PLANE_TOKEN not configured — cannot trigger retrain")
    body: dict[str, Any] = {
        "model_name": model_name,
        "dataset_name": dataset_name,
        "is_dummy": bool(dummy),
    }
    if backend_name:
        body["backend_name"] = backend_name
    try:
        data = _client.post(f"{cfg.control_plane_url}/retrain", body, token=cfg.control_plane_token)
    except _client.ClientError as exc:
        return _err(str(exc), status=getattr(exc, "status", None))
    return {"ok": True, **_as_dict(data)}


def retrain_status(flow_run_id: str) -> dict[str, Any]:
    """Poll the status of a retraining flow run started by the trigger_retrain tool.

    Args:
        flow_run_id: The flow run id returned by ``trigger_retrain``.
    """
    cfg = _cfg()
    q = urllib.parse.quote(flow_run_id)
    res = _get(f"{cfg.control_plane_url}/retrain/{q}", token=cfg.control_plane_token)
    if not res.get("ok"):
        return res
    return {"ok": True, **_as_dict(res["data"])}


# ── registry ──────────────────────────────────────────────────────────────────


# ── HPC fleet tools (Phase 35d) ───────────────────────────────────────────────


def hpc_clusters() -> dict[str, Any]:
    """List registered HPC clusters and their approval state (PENDING/ACTIVE/REJECTED).

    Reads the fleet registry (clusters.yaml + hpc_clusters). Use before scheduling to see
    which clusters a sysadmin has approved for training runs.
    """
    try:
        from examlops.hpc_registry import list_clusters

        return {"ok": True, "clusters": list_clusters()}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_nodes(cluster: str | None = None) -> dict[str, Any]:
    """Return the latest discovered node inventory (CPUs/memory/GPUs/state) for a cluster.

    Reads the ``hpc_nodes`` snapshot table. Refresh snapshots with
    ``exa hpc nodes --save --cluster <name>``.
    """
    try:
        from examlops.platform_db import get_node_snapshot, init_db

        init_db()
        return {"ok": True, "nodes": get_node_snapshot(cluster)}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_place(gpus: int = 0, nodes: int = 1) -> dict[str, Any]:
    """Recommend which ACTIVE cluster should run a job needing ``gpus``/``nodes``.

    Returns the chosen cluster, a human-readable reason, and the scored candidate list. Does
    not submit anything — it only advises placement.
    """
    try:
        from examlops.hpc_placement import ResourceAsk, choose_cluster
        from examlops.hpc_registry import active_clusters_with_inventory

        result = choose_cluster(
            ResourceAsk(gpus=gpus, nodes=nodes), active_clusters_with_inventory()
        )
        return {"ok": True, **result.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_jobs(model: str | None = None) -> dict[str, Any]:
    """List tracked HPC job submissions (from the ``hpc_jobs`` table), newest first."""
    try:
        from examlops.platform_db import get_hpc_jobs, init_db

        init_db()
        return {"ok": True, "jobs": get_hpc_jobs(model)[:50]}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


def hpc_approve_cluster(name: str) -> dict[str, Any]:
    """Approve a PENDING HPC cluster so exaMLOps may schedule jobs on it (mutating, audited).

    This is the sysadmin approval gate. Only registered when writes are explicitly enabled.
    """
    try:
        from examlops.hpc_registry import get_merged
        from examlops.platform_db import set_cluster_state, write_audit_event

        if get_merged(name) is None:
            return _err(f"unknown cluster: {name}")
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "mcp-agent"
        set_cluster_state(name, "ACTIVE", approved_by=actor)
        write_audit_event("mcp", actor, "cluster_approved", name, {"via": "mcp"})
        return {"ok": True, "cluster": name, "state": "ACTIVE"}
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))


@dataclass(frozen=True)
class ToolSpec:
    """Metadata describing one agent-callable tool."""

    fn: Callable[..., dict[str, Any]]
    mutating: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return self.fn.__name__

    @property
    def description(self) -> str:
        doc = (self.fn.__doc__ or "").strip()
        # First paragraph is the summary agents see in tool listings.
        return doc.split("\n\n", 1)[0].replace("\n", " ").strip() if doc else self.name


REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(platform_status, tags=("read", "status")),
    ToolSpec(list_models, tags=("read", "registry")),
    ToolSpec(model_detail, tags=("read", "registry")),
    ToolSpec(list_production_models, tags=("read", "registry")),
    ToolSpec(list_approvals, tags=("read", "governance")),
    ToolSpec(modelzoo_status, tags=("read", "modelzoo")),
    ToolSpec(recent_audit_events, tags=("read", "governance")),
    ToolSpec(trigger_retrain, mutating=True, tags=("write", "training")),
    ToolSpec(retrain_status, tags=("read", "training")),
    ToolSpec(hpc_clusters, tags=("read", "hpc")),
    ToolSpec(hpc_nodes, tags=("read", "hpc")),
    ToolSpec(hpc_place, tags=("read", "hpc")),
    ToolSpec(hpc_jobs, tags=("read", "hpc")),
    ToolSpec(hpc_approve_cluster, mutating=True, tags=("write", "hpc", "governance")),
)


def iter_tools(include_writes: bool | None = None) -> Iterator[ToolSpec]:
    """Yield registered tools.

    Args:
        include_writes: Whether to include mutating tools. When ``None`` (default), the
            value is read from the ``EXAMLOPS_MCP_ALLOW_WRITES`` environment variable.
    """
    if include_writes is None:
        include_writes = _writes_enabled()
    for spec in REGISTRY:
        if spec.mutating and not include_writes:
            continue
        yield spec


def _writes_enabled() -> bool:
    return os.getenv("EXAMLOPS_MCP_ALLOW_WRITES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
