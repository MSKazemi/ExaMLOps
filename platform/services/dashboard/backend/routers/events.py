"""Events console (Phase 1 item 1.3, NovaFabric event backbone · Platform group).

Surfaces the `exa events` CLI capability — the transactional-outbox event backbone — in the
dashboard over pure `platform.db` state (no live broker).

Reads (viewer): outbox backlog by state (pending/published/poison), via the same
`examlops.data.events.outbox_stats` code path `exa events stats` uses. Writes (admin +
`events.manage`, audited `source=dashboard`): enqueue an event to the outbox via the same
`examlops.events.publish` code path `exa events publish` uses. Enqueue only; the broker `relay`
needs a live broker and is out of scope for the UI.
"""

from __future__ import annotations

import json
import os

import audit_write
from auth import require_role
from capabilities import EVENTS_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/v1/events", tags=["events"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, EVENTS_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, EVENTS_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_events():
    """Lazy, guarded import of the shared event-backbone code paths (503 if unavailable)."""
    try:
        from examlops import events as _events  # type: ignore
        from examlops.data.events import outbox_stats as _outbox_stats  # type: ignore

        return _events, _outbox_stats
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "event features require the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def get_events(_=Depends(_viewer)) -> dict:
    """Outbox backlog by state. Mirrors `exa events stats` via `examlops.data.events.outbox_stats`.

    Fail-open: an unreachable outbox returns zeroed counts (never a 500)."""
    _, outbox_stats = _examlops_events()
    empty = {"pending": 0, "published": 0, "poison": 0}
    try:
        from examlops.data import init_db

        init_db()
        stats = outbox_stats()
    except Exception:
        return {"stats": empty, "total": 0}
    total = sum(int(v) for v in stats.values())
    return {"stats": stats, "total": total}


@router.post("")
async def publish_event(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Enqueue an event to the outbox (admin; audited `source=dashboard`).

    Body: ``{topic, payload?}``. Mirrors ``exa events publish`` via the shared
    ``examlops.events.publish`` (durable; the broker relay is run out-of-band, not from the UI).
    """
    _require_manage(principal)
    topic = (payload.get("topic") or "").strip()
    if not topic:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "topic is required")

    raw = payload.get("payload", {})
    if isinstance(raw, str):
        try:
            event_payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be valid JSON") from exc
    elif isinstance(raw, dict):
        event_payload = raw
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be a JSON object")

    events, _ = _examlops_events()
    event_id = events.publish(topic, event_payload)
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "event_published",
            topic,
            {"id": event_id, "topic": topic},
        )
        conn.commit()
        conn.close()
        return {"id": event_id, "topic": topic}
    finally:
        conn.close()
