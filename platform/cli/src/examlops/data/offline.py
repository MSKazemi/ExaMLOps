"""examlops.data.offline - storage for offline (batch) inference jobs (ADR 0149).

Policy (spec, executor, resume rules) lives in :mod:`examlops.offline`; this module only touches
``offline_jobs``. A job's identity is a hash of ``(tenant, idempotency key)``, so a retry after a
crash finds its own row; the row records the spec hash so the same key cannot be reused for a
different request.
"""

from __future__ import annotations

import json
import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = [
    "cancel_requested",
    "claim",
    "finish",
    "get",
    "init_db",
    "list_jobs",
    "progress",
    "request_cancel",
    "set_total",
]

_ACTIVE = ("queued", "running")


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    if d.get("result_json"):
        d["result"] = json.loads(d["result_json"])
    return d


def get(job_id: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            r = conn.execute("SELECT * FROM offline_jobs WHERE job_id=?", (job_id,)).fetchone()
        return _row(r) if r else None

    return write_retry(_do)


def claim(
    job_id: str,
    *,
    tenant: str,
    key: str,
    spec_hash: str,
    spec: dict[str, Any],
    kind: str,
    model: str,
    model_version: str | None,
    actor: str | None,
    lease_ttl_s: float,
) -> tuple[str, dict[str, Any]]:
    """Take (or retake) the job for a runner. ``(outcome, row)`` with outcome one of

    ``claimed`` (new row, run from the start), ``resumed`` (an earlier attempt is resumed),
    ``completed`` (already done: replay it), ``conflict`` (same key, different spec) or
    ``in_progress`` (another runner holds an unexpired lease).
    """

    def _do() -> tuple[str, dict[str, Any]]:
        init_db()
        now = time.time()
        with _immediate_write("offline_jobs") as conn:
            r = conn.execute("SELECT * FROM offline_jobs WHERE job_id=?", (job_id,)).fetchone()
            if r is None:
                conn.execute(
                    "INSERT INTO offline_jobs (job_id, tenant, idempotency_key, spec_hash, "
                    "spec_json, kind, model, model_version, state, lease_expires, attempts, "
                    "actor, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, 1, ?, ?, ?)",
                    (
                        job_id,
                        tenant,
                        key,
                        spec_hash,
                        json.dumps(spec, sort_keys=True),
                        kind,
                        model,
                        model_version,
                        now + lease_ttl_s,
                        actor,
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM offline_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                return "claimed", _row(row)
            row = _row(r)
            if row["spec_hash"] != spec_hash:
                return "conflict", row
            if row["state"] == "completed":
                return "completed", row
            if row["state"] == "running" and (row["lease_expires"] or 0) > now:
                return "in_progress", row
            conn.execute(
                "UPDATE offline_jobs SET state='running', cancel_requested=0, lease_expires=?, "
                "attempts=attempts+1, error=NULL, updated_at=? WHERE job_id=?",
                (now + lease_ttl_s, now, job_id),
            )
            row = _row(
                conn.execute("SELECT * FROM offline_jobs WHERE job_id=?", (job_id,)).fetchone()
            )
            return "resumed", row

    return write_retry(_do)


def cancel_requested(job_id: str) -> bool:
    def _do() -> bool:
        with get_db() as conn:
            r = conn.execute(
                "SELECT cancel_requested FROM offline_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return bool(r and r["cancel_requested"])

    return write_retry(_do)


def set_total(job_id: str, batches_total: int) -> None:
    def _do() -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE offline_jobs SET batches_total=? WHERE job_id=?", (batches_total, job_id)
            )

    write_retry(_do)


def progress(
    job_id: str, *, batches_done: int, rows_ok: int, rows_err: int, lease_ttl_s: float
) -> bool:
    """Record progress, renew the lease, and return whether a cancel was requested."""

    def _do() -> bool:
        now = time.time()
        with get_db() as conn:
            conn.execute(
                "UPDATE offline_jobs SET batches_done=?, rows_ok=?, rows_err=?, lease_expires=?, "
                "updated_at=? WHERE job_id=? AND state='running'",
                (batches_done, rows_ok, rows_err, now + lease_ttl_s, now, job_id),
            )
            r = conn.execute(
                "SELECT cancel_requested FROM offline_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return bool(r and r["cancel_requested"])

    return write_retry(_do)


def finish(
    job_id: str,
    state: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    output_revision: str | None = None,
    output_uri: str | None = None,
) -> None:
    def _do() -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE offline_jobs SET state=?, result_json=COALESCE(?, result_json), error=?, "
                "output_revision=COALESCE(?, output_revision), output_uri=COALESCE(?, output_uri), "
                "lease_expires=NULL, updated_at=? WHERE job_id=?",
                (
                    state,
                    json.dumps(result, default=str) if result is not None else None,
                    error,
                    output_revision,
                    output_uri,
                    time.time(),
                    job_id,
                ),
            )

    write_retry(_do)


def request_cancel(job_id: str) -> tuple[str, dict[str, Any] | None]:
    """``(outcome, row)``: ``requested`` (a live runner will stop between batches), ``cancelled``
    (nobody was running it, so it is cancelled now), ``not_cancellable`` (already finished), or
    ``not_found``."""

    def _do() -> tuple[str, dict[str, Any] | None]:
        init_db()
        now = time.time()
        with _immediate_write("offline_jobs") as conn:
            r = conn.execute("SELECT * FROM offline_jobs WHERE job_id=?", (job_id,)).fetchone()
            if r is None:
                return "not_found", None
            row = _row(r)
            if row["state"] in ("completed", "cancelled"):
                return "not_cancellable", row
            live = row["state"] == "running" and (row["lease_expires"] or 0) > now
            if live:
                conn.execute(
                    "UPDATE offline_jobs SET cancel_requested=1, updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
                outcome = "requested"
            else:
                conn.execute(
                    "UPDATE offline_jobs SET state='cancelled', lease_expires=NULL, "
                    "cancel_requested=0, updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
                outcome = "cancelled"
            row = _row(
                conn.execute("SELECT * FROM offline_jobs WHERE job_id=?", (job_id,)).fetchone()
            )
            return outcome, row

    return write_retry(_do)


def list_jobs(*, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT * FROM offline_jobs"
        args: list[Any] = []
        if state:
            sql += " WHERE state=?"
            args.append(state)
        sql += " ORDER BY created_at DESC, job_id LIMIT ?"
        args.append(int(limit))
        with get_db() as conn:
            return [_row(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)
