"""examlops.data.dataplane — Dataplane source catalog + pull history (ADR 0130 §6).

Owns these helpers (bodies live here, not in ``platform_db``) — per-domain split (item 4.5), a
brand-new domain module (not a monolith relocation). DDL for ``dataplane_sources`` /
``dataplane_pulls`` lives once in ``platform_db.init_db``; this module only reads/writes rows.
``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping to the mutating helpers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from secrets import token_hex
from typing import Any

from examlops.platform_db import get_db, init_db, install_write_retry

__all__ = [
    "upsert_source",
    "get_source",
    "list_sources",
    "delete_source",
    "new_pull_id",
    "insert_pull",
    "update_pull",
    "get_pull",
    "list_pulls",
    "last_pull",
]

_COMMITTED = ("succeeded", "unchanged")


def _parse(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for col, key in (
        ("spec_json", "spec"),
        ("limits_json", "limits"),
        ("watermark_json", "watermark"),
    ):
        if col in d:
            raw = d.pop(col)
            d[key] = json.loads(raw) if raw else ({} if key != "watermark" else None)
    return d


def upsert_source(
    project: str,
    name: str,
    *,
    connector: str,
    connection: str | None,
    spec: dict[str, Any],
    schedule: str | None,
    limits: dict[str, Any],
    contract: str | None,
    enabled: bool,
    actor: str | None,
) -> None:
    """Create or update a dataplane source row (upsert on ``(project, name)``)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO dataplane_sources
                   (project, name, connector, connection, spec_json, schedule, limits_json,
                    contract, enabled, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project, name) DO UPDATE SET
                   connector=excluded.connector, connection=excluded.connection,
                   spec_json=excluded.spec_json, schedule=excluded.schedule,
                   limits_json=excluded.limits_json, contract=excluded.contract,
                   enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP""",
            (
                project,
                name,
                connector,
                connection,
                json.dumps(spec, sort_keys=True),
                schedule,
                json.dumps(limits, sort_keys=True),
                contract,
                1 if enabled else 0,
                actor,
            ),
        )


def get_source(name: str, project: str = "") -> dict[str, Any] | None:
    """Fetch one source row by ``(project, name)``, or ``None`` if it does not exist."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM dataplane_sources WHERE project=? AND name=?", (project, name)
        ).fetchone()
    return _parse(row) if row else None


def list_sources(project: str | None = None) -> list[dict[str, Any]]:
    """List sources, optionally scoped to one project. ``None`` lists across all projects."""
    init_db()
    with get_db() as conn:
        if project is None:
            rows = conn.execute("SELECT * FROM dataplane_sources ORDER BY project, name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dataplane_sources WHERE project=? ORDER BY name", (project,)
            ).fetchall()
    return [_parse(r) for r in rows]


def delete_source(name: str, project: str = "") -> bool:
    """Delete a source row. Returns ``True`` if a row was deleted."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM dataplane_sources WHERE project=? AND name=?", (project, name)
        )
        return cur.rowcount > 0


_pull_id_lock = threading.Lock()
_last_pull_id_ns = 0


def new_pull_id() -> str:
    """Time-ordered id: 16 hex chars of epoch-ns (strictly increasing per process) + 6 random hex chars.

    A plain ``time.time_ns()`` can repeat (or even go backwards, on some clocks) across two calls
    made in quick succession, which would break the ordering callers rely on (``first < second``,
    and a later task parses ``int(pid[:16], 16)`` as nanoseconds for orphan age). Guarding a
    monotonically-advancing counter with a lock makes each id in this process strictly greater
    than the last, regardless of wall-clock resolution or repeated ``time.time_ns()`` reads.
    """
    global _last_pull_id_ns
    with _pull_id_lock:
        now = max(time.time_ns(), _last_pull_id_ns + 1)
        _last_pull_id_ns = now
    return f"{now:016x}{token_hex(3)}"


def insert_pull(
    pull_id: str,
    project: str,
    source: str,
    *,
    trigger_kind: str,
    actor: str | None,
    parent_revision: str | None,
) -> None:
    """Record a new pull as ``running``."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO dataplane_pulls
                   (id, project, source, status, trigger_kind, actor, parent_revision)
               VALUES (?,?,?,?,?,?,?)""",
            (pull_id, project, source, "running", trigger_kind, actor, parent_revision),
        )


def update_pull(
    pull_id: str,
    *,
    status: str,
    finished: bool = False,
    revision: str | None = None,
    row_count: int | None = None,
    byte_count: int | None = None,
    watermark: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Update a pull's status and, optionally, its outcome fields."""
    init_db()
    sets = ["status=?"]
    args: list[Any] = [status]
    for col, val in (
        ("revision", revision),
        ("row_count", row_count),
        ("byte_count", byte_count),
        ("error", error),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if watermark is not None:
        sets.append("watermark_json=?")
        args.append(json.dumps(watermark, sort_keys=True, default=str))
    if finished:
        sets.append("finished_at=CURRENT_TIMESTAMP")
    args.append(pull_id)
    sql = f"UPDATE dataplane_pulls SET {', '.join(sets)} WHERE id=?"  # noqa: S608 - fixed column names
    with get_db() as conn:
        conn.execute(sql, args)


def get_pull(pull_id: str) -> dict[str, Any] | None:
    """Fetch one pull row by id, or ``None`` if it does not exist."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM dataplane_pulls WHERE id=?", (pull_id,)).fetchone()
    return _parse(row) if row else None


def list_pulls(
    *, project: str | None = None, source: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List pulls, newest first, optionally filtered by project and/or source."""
    init_db()
    where: list[str] = []
    args: list[Any] = []
    if project is not None:
        where.append("project=?")
        args.append(project)
    if source is not None:
        where.append("source=?")
        args.append(source)
    sql = "SELECT * FROM dataplane_pulls"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()  # noqa: S608 - fixed column names
    return [_parse(r) for r in rows]


def last_pull(project: str, source: str, *, committed_only: bool = True) -> dict[str, Any] | None:
    """The most recent pull for ``(project, source)``, or the most recent committed one."""
    init_db()
    sql = "SELECT * FROM dataplane_pulls WHERE project=? AND source=?"
    args: list[Any] = [project, source]
    if committed_only:
        sql += " AND status IN (?, ?)"
        args.extend(_COMMITTED)
    sql += " ORDER BY id DESC LIMIT 1"
    with get_db() as conn:
        row = conn.execute(sql, args).fetchone()
    return _parse(row) if row else None


install_write_retry(__name__)
