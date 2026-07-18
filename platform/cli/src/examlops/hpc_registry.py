"""
HPC cluster registry — ``clusters.yaml`` (definitions) + ``hpc_clusters`` (state).

Two sources of truth, by design:

* **`clusters.yaml`** is the human-editable definition of *how to reach* each cluster
  (scheduler, transport, host, ssh user/key/port). Default location
  ``~/.config/examlops/clusters.yaml``; override with ``EXAMLOPS_HPC_REGISTRY``.
* **`hpc_clusters` table** holds the *governance state* — ``PENDING`` | ``ACTIVE`` |
  ``REJECTED`` — plus who requested/approved it and the last discovered capabilities.

A cluster is usable for scheduling only when its state is ``ACTIVE`` (a sysadmin approved
it). Merely appearing in ``clusters.yaml`` grants nothing — it still starts ``PENDING``.
This module never connects or submits; it only reads/writes the registry and resolves an
approved cluster into the ``EXAMLOPS_HPC_*`` environment the Phase 23 adapter already reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from examlops.data import init_db
from examlops.data.hpc import get_cluster, get_clusters, get_node_snapshot, upsert_cluster

# Definition fields carried in clusters.yaml (never secrets — key is a path reference).
_DEF_FIELDS = ("scheduler", "transport", "host", "ssh_user", "ssh_port", "ssh_key")


def registry_path() -> Path:
    env = os.getenv("EXAMLOPS_HPC_REGISTRY")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "examlops" / "clusters.yaml"


def default_cluster() -> str | None:
    """The cluster to target when none is passed explicitly (``EXAMLOPS_HPC_CLUSTER``)."""
    return os.getenv("EXAMLOPS_HPC_CLUSTER") or None


# ── clusters.yaml I/O ─────────────────────────────────────────────────────────────


def _load_yaml() -> dict[str, dict]:
    path = registry_path()
    if not path.exists():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    clusters = data.get("clusters", data) if isinstance(data, dict) else {}
    # Accept either {name: {...}} or a list of {name: ..., ...}.
    if isinstance(clusters, list):
        return {c["name"]: {k: v for k, v in c.items() if k != "name"} for c in clusters}
    return dict(clusters)


def _save_yaml(clusters: dict[str, dict]) -> None:
    import yaml

    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"clusters": clusters}, sort_keys=True))


def add_yaml_cluster(name: str, definition: dict) -> None:
    """Merge one cluster definition into clusters.yaml (creating the file if needed)."""
    clusters = _load_yaml()
    clusters[name] = {k: v for k, v in definition.items() if k in _DEF_FIELDS and v is not None}
    _save_yaml(clusters)


# ── merged view (definition + state) ──────────────────────────────────────────────


def _merge_one(name: str, yaml_def: dict | None, db_row: dict | None) -> dict:
    yaml_def = yaml_def or {}
    db_row = db_row or {}
    merged: dict[str, Any] = {"name": name}
    for f in _DEF_FIELDS:
        merged[f] = yaml_def.get(f) if yaml_def.get(f) is not None else db_row.get(f)
    merged["state"] = db_row.get("state") or "PENDING"
    merged["approved_by"] = db_row.get("approved_by")
    merged["requested_by"] = db_row.get("requested_by")
    merged["reason"] = db_row.get("reason")
    merged["capabilities"] = db_row.get("capabilities")
    return merged


def list_clusters() -> list[dict]:
    """Union of clusters.yaml and the DB, each with resolved definition + state."""
    init_db()
    yaml_clusters = _load_yaml()
    db_clusters = {c["name"]: c for c in get_clusters()}
    names = sorted(set(yaml_clusters) | set(db_clusters))
    return [_merge_one(n, yaml_clusters.get(n), db_clusters.get(n)) for n in names]


def get_merged(name: str) -> dict | None:
    yaml_clusters = _load_yaml()
    db_row = get_cluster(name)
    if name not in yaml_clusters and db_row is None:
        return None
    return _merge_one(name, yaml_clusters.get(name), db_row)


# ── connect (write PENDING to both sides) ─────────────────────────────────────────


def register_pending(
    name: str,
    scheduler: str,
    *,
    transport: str = "ssh",
    host: str | None = None,
    ssh_user: str | None = None,
    ssh_port: int | None = 22,
    ssh_key: str | None = None,
    key_fingerprint: str | None = None,
    capabilities: dict | None = None,
    requested_by: str | None = None,
) -> None:
    """Write a discovered cluster to clusters.yaml + the DB (state defaults PENDING)."""
    init_db()
    add_yaml_cluster(
        name,
        {
            "scheduler": scheduler,
            "transport": transport,
            "host": host,
            "ssh_user": ssh_user,
            "ssh_port": ssh_port,
            "ssh_key": ssh_key,
        },
    )
    upsert_cluster(
        name,
        scheduler,
        transport=transport,
        host=host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_key=ssh_key,
        key_fingerprint=key_fingerprint,
        capabilities=capabilities,
        requested_by=requested_by,
    )


# ── resolution for scheduling ─────────────────────────────────────────────────────


class ClusterNotActiveError(RuntimeError):
    """Raised when a scheduling action targets a cluster that is not ACTIVE."""


def require_active(name: str) -> dict:
    """Return the merged cluster iff it exists and is ACTIVE, else raise."""
    merged = get_merged(name)
    if merged is None:
        raise ClusterNotActiveError(f"unknown cluster: {name!r} (run 'exa hpc connect' first)")
    if merged["state"] != "ACTIVE":
        raise ClusterNotActiveError(
            f"cluster {name!r} is {merged['state']} — a sysadmin must approve it "
            f"('exa hpc approve {name}') before jobs can be scheduled on it"
        )
    return merged


def active_clusters_with_inventory() -> list[dict]:
    """ACTIVE clusters shaped for the placement engine (capabilities + node snapshot)."""
    init_db()
    out: list[dict] = []
    for c in get_clusters("ACTIVE"):
        caps = None
        if c.get("capabilities"):
            try:
                caps = json.loads(c["capabilities"])
            except (ValueError, TypeError):
                caps = None
        out.append(
            {
                "name": c["name"],
                "scheduler": c["scheduler"],
                "capabilities": caps,
                "nodes": get_node_snapshot(c["name"]),
            }
        )
    return out


def resolve_env(name: str) -> dict[str, str]:
    """Build the ``EXAMLOPS_HPC_*`` env for an ACTIVE cluster (for ``exa pipeline run``)."""
    c = require_active(name)
    env: dict[str, str] = {}
    scheduler = c.get("scheduler")
    if scheduler in ("flux", "slurm", "mock"):
        env["EXAMLOPS_HPC_SCHEDULER"] = scheduler
    elif scheduler == "unmanaged":
        env["EXAMLOPS_HPC_SCHEDULER"] = "mock"
    transport = c.get("transport") or ("ssh" if c.get("host") else "local")
    env["EXAMLOPS_HPC_TRANSPORT"] = transport
    if transport == "ssh" and c.get("host"):
        env["EXAMLOPS_HPC_SSH_HOST"] = str(c["host"])
        if c.get("ssh_user"):
            env["EXAMLOPS_HPC_SSH_USER"] = str(c["ssh_user"])
        if c.get("ssh_key"):
            env["EXAMLOPS_HPC_SSH_KEY"] = str(c["ssh_key"])
        if c.get("ssh_port"):
            env["EXAMLOPS_HPC_SSH_PORT"] = str(c["ssh_port"])
    return env
