"""examlops.data.hpc — HPC fleet.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import datetime
import json
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "aggregate_node_capacity",
    "get_cluster",
    "get_clusters",
    "get_hpc_jobs",
    "get_node_snapshot",
    "list_placement_decisions",
    "record_hpc_job",
    "record_node_snapshot",
    "record_placement_decision",
    "set_cluster_state",
    "update_hpc_job",
    "upsert_cluster",
]


def aggregate_node_capacity(cluster: str | None = None) -> dict[str, dict[str, Any]]:
    """SQL-side per-cluster capacity aggregation (Phase 1 item 1.9).

    Instead of loading every ``hpc_nodes`` row into Python (O(nodes) memory — thousands at fleet
    scale) and summing there, this ``GROUP BY cluster, state`` runs in SQLite over the
    ``ix_hpc_nodes_cluster_state`` index and returns only the per-cluster rollup. Output shape
    matches :func:`examlops.hpc_placement.node_capacity` per cluster::

        {"<cluster>": {total_nodes, idle_nodes, total_gpus, idle_gpus, idle_cpus, by_state}}
    """
    where = "WHERE cluster = ?" if cluster else ""
    params = (cluster,) if cluster else ()
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT cluster,
                       COALESCE(state, 'unknown') AS state,
                       COUNT(*)                   AS nodes,
                       COALESCE(SUM(gpus), 0)     AS gpus,
                       COALESCE(SUM(cpus), 0)     AS cpus
                  FROM hpc_nodes {where}
                 GROUP BY cluster, COALESCE(state, 'unknown')""",
            params,
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        agg = out.setdefault(
            r["cluster"],
            {
                "total_nodes": 0,
                "idle_nodes": 0,
                "total_gpus": 0,
                "idle_gpus": 0,
                "idle_cpus": 0,
                "by_state": {},
            },
        )
        agg["total_nodes"] += r["nodes"]
        agg["total_gpus"] += r["gpus"]
        agg["by_state"][r["state"]] = r["nodes"]
        if r["state"] == "idle":
            agg["idle_nodes"] += r["nodes"]
            agg["idle_gpus"] += r["gpus"]
            agg["idle_cpus"] += r["cpus"]
    return out


def get_cluster(name: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM hpc_clusters WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def get_clusters(state: str | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        if state:
            rows = conn.execute(
                "SELECT * FROM hpc_clusters WHERE state=? ORDER BY name ASC", (state,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM hpc_clusters ORDER BY name ASC").fetchall()
    return [dict(r) for r in rows]


def get_hpc_jobs(model: str | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT * FROM hpc_jobs WHERE model=? ORDER BY id DESC", (model,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM hpc_jobs ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_node_snapshot(cluster: str | None = None) -> list[dict[str, Any]]:
    """Return the latest stored node inventory, optionally filtered to one cluster."""
    with get_db() as conn:
        if cluster:
            rows = conn.execute(
                "SELECT * FROM hpc_nodes WHERE cluster=? ORDER BY node ASC", (cluster,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM hpc_nodes ORDER BY cluster ASC, node ASC").fetchall()
    return [dict(r) for r in rows]


def list_placement_decisions(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM placement_decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def record_hpc_job(
    job_id: str,
    scheduler: str,
    flow_run_id: str | None,
    model: str,
    dataset: str,
    nodes: int | None = None,
    gpus: int | None = None,
    cpus: int | None = None,
    submit_time: str | None = None,
    mlflow_run_id: str | None = None,
) -> None:
    """Insert (or upsert) an HPC job tracking row.

    Idempotent on ``(scheduler, job_id)`` so a Prefect task retry that resubmits does
    not create duplicate rows.
    """

    submit_time = submit_time or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO hpc_jobs
                   (job_id, scheduler, flow_run_id, model, dataset, state,
                    submit_time, nodes, gpus, cpus, mlflow_run_id)
               VALUES (?,?,?,?,?,'SUBMITTED',?,?,?,?,?)
               ON CONFLICT(scheduler, job_id) DO UPDATE SET
                   flow_run_id=excluded.flow_run_id,
                   model=excluded.model,
                   dataset=excluded.dataset,
                   nodes=excluded.nodes,
                   gpus=excluded.gpus,
                   cpus=excluded.cpus,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                job_id,
                scheduler,
                flow_run_id,
                model,
                dataset,
                submit_time,
                nodes,
                gpus,
                cpus,
                mlflow_run_id,
            ),
        )


def record_node_snapshot(cluster: str, scheduler: str, nodes: list[dict[str, Any]]) -> int:
    """Replace the stored node inventory for ``cluster`` with a fresh snapshot.

    Snapshot semantics (latest wins): existing rows for the cluster are deleted and the
    current ``nodes`` (dicts shaped like ``discovery.NodeInfo.to_dict()``) are inserted.
    Returns the number of node rows written.
    """
    with get_db() as conn:
        conn.execute("DELETE FROM hpc_nodes WHERE cluster=?", (cluster,))
        conn.executemany(
            """INSERT INTO hpc_nodes
                   (cluster, scheduler, node, cpus, memory_mb, gpus, gpu_model, state, partition)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    cluster,
                    scheduler,
                    n.get("name"),
                    n.get("cpus"),
                    n.get("memory_mb"),
                    n.get("gpus", 0) or 0,
                    n.get("gpu_model"),
                    n.get("state"),
                    n.get("partition"),
                )
                for n in nodes
            ],
        )
    # A fresh snapshot makes any cached capacity rollup stale (item 1.9).
    try:
        from examlops.hpc_capacity import invalidate_capacity_cache

        invalidate_capacity_cache()
    except Exception:  # noqa: BLE001 - cache invalidation is best-effort
        pass
    return len(nodes)


def record_placement_decision(
    workload: str,
    *,
    accelerator_requested: str | None,
    device_chosen: str | None,
    pool: str | None,
    target: str | None,
    region: str | None,
    decision: str,
    fraction_honored: bool = True,
    reason: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO placement_decisions
                   (workload, accelerator_requested, device_chosen, pool, target, region,
                    decision, fraction_honored, reason)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                workload,
                accelerator_requested,
                device_chosen,
                pool,
                target,
                region,
                decision,
                1 if fraction_honored else 0,
                reason,
            ),
        )


def set_cluster_state(
    name: str,
    state: str,
    *,
    approved_by: str | None = None,
    reason: str | None = None,
) -> bool:
    """Transition a cluster's state (PENDING|ACTIVE|REJECTED). Returns False if unknown."""
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE hpc_clusters
                   SET state=?, approved_by=COALESCE(?, approved_by),
                       reason=COALESCE(?, reason), updated_at=CURRENT_TIMESTAMP
                 WHERE name=?""",
            (state, approved_by, reason, name),
        )
        return cur.rowcount > 0


def update_hpc_job(
    job_id: str,
    scheduler: str,
    *,
    state: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    exit_code: int | None = None,
    queue_seconds: float | None = None,
    run_seconds: float | None = None,
    mlflow_run_id: str | None = None,
) -> None:
    """Update mutable fields of an existing hpc_jobs row (no-op if none provided)."""
    fields = {
        "state": state,
        "start_time": start_time,
        "end_time": end_time,
        "exit_code": exit_code,
        "queue_seconds": queue_seconds,
        "run_seconds": run_seconds,
        "mlflow_run_id": mlflow_run_id,
    }
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        return
    assignments = ", ".join(f"{k}=?" for k in sets)
    with get_db() as conn:
        conn.execute(
            f"UPDATE hpc_jobs SET {assignments}, updated_at=CURRENT_TIMESTAMP "
            "WHERE scheduler=? AND job_id=?",
            (*sets.values(), scheduler, job_id),
        )


def upsert_cluster(
    name: str,
    scheduler: str,
    *,
    transport: str = "ssh",
    host: str | None = None,
    ssh_user: str | None = None,
    ssh_port: int | None = 22,
    ssh_key: str | None = None,
    key_fingerprint: str | None = None,
    capabilities: dict[str, Any] | None = None,
    requested_by: str | None = None,
) -> None:
    """Insert or update a cluster *definition* — state is never changed here.

    A brand-new cluster starts ``PENDING`` (the table default). Re-running discovery on an
    already-approved (or already-rejected) cluster refreshes its definition + capabilities
    but leaves its ``state``/``approved_by`` intact, so re-probing can never silently
    authorize or de-authorize a cluster. State transitions go through
    :func:`set_cluster_state`.
    """
    caps = json.dumps(capabilities) if capabilities else None
    with get_db() as conn:
        conn.execute(
            """INSERT INTO hpc_clusters
                   (name, scheduler, transport, host, ssh_user, ssh_port, ssh_key,
                    key_fingerprint, capabilities, requested_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                   scheduler=excluded.scheduler,
                   transport=excluded.transport,
                   host=excluded.host,
                   ssh_user=excluded.ssh_user,
                   ssh_port=excluded.ssh_port,
                   ssh_key=excluded.ssh_key,
                   key_fingerprint=excluded.key_fingerprint,
                   capabilities=excluded.capabilities,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                name,
                scheduler,
                transport,
                host,
                ssh_user,
                ssh_port,
                ssh_key,
                key_fingerprint,
                caps,
                requested_by,
            ),
        )


install_write_retry(__name__)
