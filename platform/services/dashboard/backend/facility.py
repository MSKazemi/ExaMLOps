"""Exascale facility-console aggregators (F6 / ADR 0059).

Scheduler-neutral, view-shaped read helpers over the shared ``platform.db`` ``hpc_jobs`` table
that back the facility-console endpoints in :mod:`routers.facility`. They render the phase-23
scheduler abstraction (mock / Slurm / Flux) — a facility overview (allocation, queue depth,
per-partition utilization), the job queue, and per-job detail with the MLflow cost link.

**Graceful degradation (F6 R7).** Missing telemetry yields ``"no data"``-shaped payloads
(zeros / empty lists / ``None``), never an error — so a fresh DB or a cluster with no jobs
renders an empty-but-valid console instead of a 500.

**Multi-cluster (F6 R6).** ``scheduler`` maps to a "cluster"; every helper accepts an optional
``scheduler`` filter so the UI's cluster switcher rescopes all lists through one code path.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import audit_write
from dbconn import connect

# Job states that count as actively holding resources vs waiting in the queue.
_RUNNING_STATES = ("RUNNING", "R", "COMPLETING")
_QUEUED_STATES = ("SUBMITTED", "PENDING", "PD", "CONFIGURING")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    return conn


def _has_hpc_jobs(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hpc_jobs'"
    ).fetchone()
    return row is not None


def _scheduler_clause(scheduler: str | None) -> tuple[str, tuple]:
    """SQL fragment + params for an optional cluster (scheduler) filter."""
    if scheduler:
        return " AND scheduler = ?", (scheduler,)
    return "", ()


# ── clusters (multi-cluster switcher, F6 R6) ─────────────────────────────────


def clusters(db_path: str) -> list[str]:
    """Distinct schedulers/clusters present in the DB (for the switcher)."""
    conn = _connect(db_path)
    try:
        if not _has_hpc_jobs(conn):
            return []
        return [
            r["scheduler"]
            for r in conn.execute("SELECT DISTINCT scheduler FROM hpc_jobs ORDER BY scheduler")
        ]
    finally:
        conn.close()


# ── fleet registry: clusters + approval gate (Phase 35b) ─────────────────────


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _capacity_by_cluster(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Idle/total GPUs per cluster from the hpc_nodes snapshot (empty if absent)."""
    if not _has_table(conn, "hpc_nodes"):
        return {}
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        """SELECT cluster,
                  SUM(gpus) AS total_gpus,
                  SUM(CASE WHEN state='idle' THEN gpus ELSE 0 END) AS idle_gpus
             FROM hpc_nodes GROUP BY cluster"""
    ):
        out[r["cluster"]] = {
            "total_gpus": r["total_gpus"] or 0,
            "idle_gpus": r["idle_gpus"] or 0,
        }
    return out


def _gpu_hours_by_scheduler(conn: sqlite3.Connection) -> dict[str, float]:
    """Consumed GPU-hours (gpus × run_seconds / 3600) per scheduler from hpc_jobs."""
    if not _has_hpc_jobs(conn):
        return {}
    out: dict[str, float] = {}
    for r in conn.execute(
        """SELECT scheduler, SUM(gpus * run_seconds) / 3600.0 AS gpu_hours
             FROM hpc_jobs
            WHERE gpus IS NOT NULL AND run_seconds IS NOT NULL
            GROUP BY scheduler"""
    ):
        out[r["scheduler"]] = round(r["gpu_hours"] or 0.0, 2)
    return out


def fleet_clusters(db_path: str) -> list[dict[str, Any]]:
    """Registered clusters with approval state + capacity/utilization (graceful if absent)."""
    conn = _connect(db_path)
    try:
        if not _has_table(conn, "hpc_clusters"):
            return []
        rows = conn.execute(
            """SELECT name, scheduler, transport, host, state, approved_by, requested_by,
                      reason, capabilities, updated_at
                 FROM hpc_clusters ORDER BY name"""
        ).fetchall()
        cap_by_cluster = _capacity_by_cluster(conn)
        gpu_hours = _gpu_hours_by_scheduler(conn)
        out: list[dict[str, Any]] = []
        for r in rows:
            caps = None
            if r["capabilities"]:
                try:
                    caps = json.loads(r["capabilities"])
                except (ValueError, TypeError):
                    caps = None
            # Prefer live node-snapshot totals; fall back to declared capabilities.
            snap = cap_by_cluster.get(r["name"], {})
            total_gpus = snap.get("total_gpus") or (caps or {}).get("total_gpus", 0) or 0
            idle_gpus = snap.get("idle_gpus", total_gpus)
            util = round(100.0 * (total_gpus - idle_gpus) / total_gpus, 1) if total_gpus else 0.0
            out.append(
                {
                    "name": r["name"],
                    "scheduler": r["scheduler"],
                    "transport": r["transport"],
                    "host": r["host"],
                    "state": r["state"],
                    "approvedBy": r["approved_by"],
                    "requestedBy": r["requested_by"],
                    "reason": r["reason"],
                    "capabilities": caps,
                    "totalGpus": total_gpus,
                    "idleGpus": idle_gpus,
                    "utilizationPct": util,
                    "gpuHoursUsed": gpu_hours.get(r["scheduler"], 0.0),
                    "updatedAt": r["updated_at"],
                }
            )
        return out
    finally:
        conn.close()


def set_cluster_state(
    db_path: str, name: str, state: str, *, actor: str, reason: str | None = None
) -> bool:
    """Transition a cluster's state and write an audit event. False if unknown/no table."""
    conn = _connect(db_path)
    try:
        if not _has_table(conn, "hpc_clusters"):
            return False
        cur = conn.execute(
            """UPDATE hpc_clusters
                   SET state=?, approved_by=?, reason=COALESCE(?, reason),
                       updated_at=CURRENT_TIMESTAMP
                 WHERE name=?""",
            (state, actor, reason, name),
        )
        if cur.rowcount == 0:
            return False
        action = "cluster_approved" if state == "ACTIVE" else "cluster_rejected"
        if _has_table(conn, "audit_events"):
            audit_write.audit(actor, action, name, {"reason": reason}, conn=conn)
        conn.commit()
        return True
    finally:
        conn.close()


# ── overview (F6 R1) ─────────────────────────────────────────────────────────


def facility_overview(db_path: str, scheduler: str | None = None) -> dict[str, Any]:
    """Facility KPIs: allocated vs idle nodes/GPUs, queue depth, per-partition utilization.

    "Allocated" sums the node/GPU asks of jobs in a running state; queue depth counts waiting
    jobs. Partitions are keyed by scheduler (the neutral notion of a cluster/partition here).
    """
    where, params = _scheduler_clause(scheduler)
    conn = _connect(db_path)
    try:
        if not _has_hpc_jobs(conn):
            return _empty_overview()

        running_ph = ",".join("?" * len(_RUNNING_STATES))
        queued_ph = ",".join("?" * len(_QUEUED_STATES))

        alloc = conn.execute(
            f"SELECT COALESCE(SUM(nodes),0) AS n, COALESCE(SUM(gpus),0) AS g, COUNT(*) AS c "
            f"FROM hpc_jobs WHERE state IN ({running_ph}){where}",
            (*_RUNNING_STATES, *params),
        ).fetchone()

        queued = conn.execute(
            f"SELECT COUNT(*) AS c FROM hpc_jobs WHERE state IN ({queued_ph}){where}",
            (*_QUEUED_STATES, *params),
        ).fetchone()

        partitions = [
            {
                "name": r["scheduler"],
                "running": r["running"],
                "queued": r["queued"],
                "gpusAllocated": r["gpus"],
            }
            for r in conn.execute(
                f"""SELECT scheduler,
                           SUM(CASE WHEN state IN ({running_ph}) THEN 1 ELSE 0 END) AS running,
                           SUM(CASE WHEN state IN ({queued_ph}) THEN 1 ELSE 0 END) AS queued,
                           COALESCE(SUM(CASE WHEN state IN ({running_ph}) THEN gpus ELSE 0 END),0) AS gpus
                    FROM hpc_jobs WHERE 1=1{where} GROUP BY scheduler ORDER BY scheduler""",
                (*_RUNNING_STATES, *_QUEUED_STATES, *_RUNNING_STATES, *params),
            )
        ]

        return {
            "nodesAllocated": alloc["n"],
            "gpusAllocated": alloc["g"],
            "jobsRunning": alloc["c"],
            "queueDepth": queued["c"],
            "clusters": clusters(db_path),
            "partitions": partitions,
        }
    finally:
        conn.close()


def _empty_overview() -> dict[str, Any]:
    return {
        "nodesAllocated": 0,
        "gpusAllocated": 0,
        "jobsRunning": 0,
        "queueDepth": 0,
        "clusters": [],
        "partitions": [],
    }


# ── queue (F6 R2) ─────────────────────────────────────────────────────────────


def job_queue(db_path: str, scheduler: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Waiting jobs, longest-waiting first (proxy for priority/backfill, F6 R2).

    ``hpc_jobs`` has no fair-share/priority column, so wait time (``queue_seconds``) is the
    ordering signal; the shape still carries the QueuedJob fields the UI expects.
    """
    where, params = _scheduler_clause(scheduler)
    conn = _connect(db_path)
    try:
        if not _has_hpc_jobs(conn):
            return []
        queued_ph = ",".join("?" * len(_QUEUED_STATES))
        rows = conn.execute(
            f"""SELECT job_id, scheduler, model, dataset, state, submit_time,
                       COALESCE(queue_seconds,0) AS wait, nodes, gpus
                FROM hpc_jobs WHERE state IN ({queued_ph}){where}
                ORDER BY wait DESC LIMIT ?""",
            (*_QUEUED_STATES, *params, limit),
        ).fetchall()
        return [
            {
                "id": r["job_id"],
                "cluster": r["scheduler"],
                "model": r["model"],
                "dataset": r["dataset"],
                "state": r["state"],
                "waitSec": round(r["wait"], 1),
                "nodes": r["nodes"],
                "gpus": r["gpus"],
                "submitTime": r["submit_time"],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ── job detail (F6 R2) ────────────────────────────────────────────────────────


def job_detail(db_path: str, job_id: str) -> dict[str, Any] | None:
    """Per-job detail: resources, timing, exit, and the MLflow cost link (``mlflow_run_id``).

    Returns ``None`` when the job is unknown (router maps that to 404).
    """
    conn = _connect(db_path)
    try:
        if not _has_hpc_jobs(conn):
            return None
        r = conn.execute(
            "SELECT * FROM hpc_jobs WHERE job_id = ? ORDER BY updated_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if r is None:
            return None
        return {
            "id": r["job_id"],
            "cluster": r["scheduler"],
            "model": r["model"],
            "dataset": r["dataset"],
            "state": r["state"],
            "resources": {"nodes": r["nodes"], "gpus": r["gpus"], "cpus": r["cpus"]},
            "timing": {
                "submit": r["submit_time"],
                "start": r["start_time"],
                "end": r["end_time"],
                "queueSeconds": r["queue_seconds"],
                "runSeconds": r["run_seconds"],
            },
            "exitCode": r["exit_code"],
            "flowRunId": r["flow_run_id"],
            "mlflowRunId": r["mlflow_run_id"],  # cost link → model_costs / MLflow
        }
    finally:
        conn.close()
