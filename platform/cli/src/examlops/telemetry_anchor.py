"""Anchored telemetry — tamper-evidence for high-volume side tables (ADR 0110 decision 2).

Lineage and resource telemetry (one row per inference / per job) cannot run through the
serialised audit hash chain without making the chain the platform's write bottleneck. Instead,
each side table is periodically **anchored**: a checkpoint hash over a rowid range is written
INTO the chain as a ``telemetry_anchor`` action event. Tampering with an anchored side-table row
breaks the anchor; the anchor event itself is protected by the chain. The cadence is whatever
calls :func:`anchor_telemetry` (the autopilot cycle does, best-effort, and ``exa audit anchor``
is cron-able) — and because each anchor names its own range, the verifier always knows exactly
which guarantee it is checking (the ADR's "cadence recorded in the chain" requirement).

Verification is deliberately a second path beside ``verify_audit_chain()`` (decision 5): links
prove the chain, anchors prove the side tables the chain vouches for.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from examlops.data import get_db, init_db

#: Side tables under anchor protection: the per-inference resource telemetry and the lineage
#: record. Each anchors independently so one table's write rate never delays another's.
ANCHORED_TABLES: tuple[str, ...] = (
    "drift_snapshots",
    "input_snapshots",
    "hpc_jobs",
    "dataset_revisions",
)

_ACTION = "telemetry_anchor"


def _table_id_column(table: str) -> str:
    return "id"


def _range_hash(conn: Any, table: str, from_id: int, to_id: int) -> tuple[str, int]:
    """SHA-256 over the canonical JSON of rows with ``from_id <= id <= to_id``, plus the count.

    Canonical form: per-row dict with sorted keys, values via ``str`` fallback, joined in id
    order. Verified against the same store that was anchored, so engine-level value rendering
    is consistent between anchor time and verify time.
    """
    idcol = _table_id_column(table)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE {idcol} >= ? AND {idcol} <= ? ORDER BY {idcol} ASC",  # noqa: S608
        (from_id, to_id),
    ).fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(dict(r), sort_keys=True, default=str).encode())
        h.update(b"\n")
    return h.hexdigest(), len(rows)


def _last_anchor(conn: Any, table: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT details FROM audit_events WHERE action=? AND target=? ORDER BY id DESC LIMIT 1",
        (_ACTION, table),
    ).fetchone()
    if row is None or not row["details"]:
        return None
    try:
        return json.loads(row["details"])
    except (TypeError, ValueError):
        return None


def anchor_telemetry(actor: str | None = None) -> list[dict[str, Any]]:
    """Anchor every table's un-anchored range into the chain. Returns one summary per table.

    Idempotent between writes: a table with no new rows since its last anchor is skipped
    (``rows: 0`` in the summary), so calling this every cycle costs one range query per table.
    """
    from examlops.data.audit import audit_best_effort

    init_db()
    out: list[dict[str, Any]] = []
    for table in ANCHORED_TABLES:
        with get_db() as conn:
            last = _last_anchor(conn, table)
            from_id = int(last["to_id"]) + 1 if last else 1
            row = conn.execute(
                f"SELECT MAX({_table_id_column(table)}) AS m FROM {table}"  # noqa: S608
            ).fetchone()
            max_id = int(row["m"]) if row and row["m"] is not None else 0
            if max_id < from_id:
                out.append({"table": table, "rows": 0, "anchored": False})
                continue
            digest, count = _range_hash(conn, table, from_id, max_id)
        details = {
            "table": table,
            "from_id": from_id,
            "to_id": max_id,
            "rows": count,
            "sha256": digest,
        }
        audit_best_effort("audit", actor, _ACTION, table, details)
        out.append({**details, "anchored": True})
    return out


def verify_anchors() -> dict[str, Any]:
    """Recompute every anchor's range hash and report breaks (ADR 0110 decision 5).

    A break means the side table's rows no longer match what the chain vouched for — edited,
    deleted, or inserted inside an anchored range. Rows past the newest anchor are counted as
    ``unanchored`` (they must be *reported*, never silently skipped — the same lesson as the
    unchained-events count in ``verify_audit_chain``).
    """
    init_db()
    breaks: list[dict[str, Any]] = []
    pruned: list[dict[str, Any]] = []
    checked = 0
    unanchored: dict[str, int] = {}
    with get_db() as conn:
        # Retention pruning legitimately deletes old telemetry rows and is itself audited
        # (`telemetry_pruned`). An anchor older than a recorded prune whose range LOST rows is
        # reported as pruned, not as tampering — the chain accounts for the absence. An edit
        # (hash mismatch with the row count intact) is still a break regardless.
        prune_row = conn.execute(
            "SELECT MAX(id) AS m FROM audit_events WHERE action='telemetry_pruned'"
        ).fetchone()
        last_prune_event_id = int(prune_row["m"]) if prune_row and prune_row["m"] else 0
        rows = conn.execute(
            "SELECT id, target, details FROM audit_events WHERE action=? ORDER BY id ASC",
            (_ACTION,),
        ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["details"])
            except (TypeError, ValueError):
                breaks.append({"event_id": r["id"], "table": r["target"], "reason": "unreadable"})
                continue
            table = d.get("table")
            if table not in ANCHORED_TABLES:
                continue
            checked += 1
            digest, count = _range_hash(conn, table, int(d["from_id"]), int(d["to_id"]))
            if digest != d.get("sha256") or count != int(d.get("rows", -1)):
                count_changed = count != int(d.get("rows", -1))
                entry = {
                    "event_id": r["id"],
                    "table": table,
                    "from_id": d["from_id"],
                    "to_id": d["to_id"],
                    "reason": "row count changed" if count_changed else "hash mismatch",
                }
                if count_changed and r["id"] < last_prune_event_id:
                    pruned.append(entry)
                else:
                    breaks.append(entry)
        for table in ANCHORED_TABLES:
            last = _last_anchor(conn, table)
            from_id = int(last["to_id"]) + 1 if last else 1
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {_table_id_column(table)} >= ?",  # noqa: S608
                (from_id,),
            ).fetchone()
            n = int(row["n"]) if row else 0
            if n:
                unanchored[table] = n
    return {
        "ok": not breaks,
        "anchors_checked": checked,
        "breaks": breaks,
        "pruned_anchors": pruned,
        "unanchored_rows": unanchored,
    }
