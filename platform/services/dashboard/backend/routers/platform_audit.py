"""Platform audit log — reads audit_events from shared platform.db."""

from __future__ import annotations

import json
import logging
import os

from auth import require_role
from dbconn import connect
from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform-audit", tags=["platform-audit"])

_admin = require_role("admin")
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("")
async def get_platform_audit(
    _=Depends(_admin),
    last_days: int | None = Query(
        None,
        ge=1,
        le=3650,
        description="Restrict to the last N days. Omit for the full history (default).",
    ),
    model: str | None = Query(None),
    action: str | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    """Read platform audit_events from shared platform.db.

    Defaults to the **full** audit history (no time filter) so events older than any fixed
    window still surface — the Audit page previously hardcoded a 30-day window and appeared
    empty whenever all events predated it, even though the governance chain counted them
    (the P0 "audit shows 0 while governance shows N" bug). Pass ``last_days`` to narrow.

    Errors are surfaced (HTTP 500), never masked as an empty result — an unreadable DB or a
    schema problem must not look identical to "no audit activity".
    """
    try:
        conn = connect(_db_path())
        try:
            query = (
                "SELECT id, ts, source, actor, action, target, details FROM audit_events WHERE 1=1"
            )
            params: list = []
            if last_days is not None:
                query += " AND ts >= datetime('now', ?)"
                params.append(f"-{last_days} days")
            if model:
                query += " AND target=?"
                params.append(model)
            if action:
                query += " AND action=?"
                params.append(action)
            if source:
                query += " AND source=?"
                params.append(source)
            query += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            conn.close()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("platform audit query failed")
        raise HTTPException(status_code=500, detail="Failed to read platform audit log") from exc

    def _parse_details(raw: str | None):
        # Best-effort: a single malformed/legacy `details` value (non-JSON) must not
        # 500 the whole audit page — return it verbatim rather than raising. This runs
        # OUTSIDE the query try/except, so an unguarded json.loads here crashed the
        # endpoint whenever any older row had non-JSON details (the limit=100 500 bug).
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw

    items = [
        {
            "id": r["id"],
            "ts": r["ts"],
            "source": r["source"],
            "actor": r["actor"],
            "action": r["action"],
            "target": r["target"],
            "details": _parse_details(r["details"]),
        }
        for r in rows
    ]
    return {"items": items, "total": len(items)}
