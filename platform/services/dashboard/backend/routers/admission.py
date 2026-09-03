"""Admission console (Phase 1 item 1.5, dashboard-enterprise-rebuild · Operate group).

Surfaces the `exa admission` CLI capability — the durable, per-tenant fair-share admission-control
queue — in the dashboard over pure `platform.db` state (no live infra).

Reads (viewer): queue depth by state (queued/running/done/rejected/failed), via the same
`examlops.admission.stats` code path `exa admission stats` uses. Writes (admin + `admission.manage`,
audited `source=dashboard`): enqueue a work item via `examlops.admission.submit` — the same code path
`exa admission submit` uses. Enqueue only; the worker drain is out of scope for the UI.
"""

from __future__ import annotations

import json
import os

import audit_write
from auth import require_role
from capabilities import ADMISSION_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/v1/admission", tags=["admission"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, ADMISSION_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, ADMISSION_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_admission():
    """Lazy, guarded import of the shared admission code path (503 if unavailable)."""
    try:
        from examlops import admission as _a  # type: ignore

        return _a
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "admission features require the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def get_admission(_=Depends(_viewer)) -> dict:
    """Queue depth by state. Mirrors `exa admission stats` via `examlops.admission.stats`.

    Fail-open: an unreachable queue returns zeroed counts (never a 500)."""
    admission = _examlops_admission()
    empty = {"queued": 0, "running": 0, "done": 0, "rejected": 0, "failed": 0}
    try:
        stats = admission.stats()
    except Exception:
        return {"stats": empty, "total": 0}
    total = sum(int(v) for v in stats.values())
    return {"stats": stats, "total": total}


@router.post("")
async def submit_admission(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Enqueue a work item into the admission-control queue (admin; audited `source=dashboard`).

    Body: ``{kind, payload?, tenant?, project?, priority?}``. Mirrors ``exa admission submit`` via the
    shared ``examlops.admission.submit``.
    """
    _require_manage(principal)
    kind = (payload.get("kind") or "").strip()
    if not kind:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind is required")

    raw = payload.get("payload", {})
    if isinstance(raw, str):
        try:
            item_payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be valid JSON") from exc
    elif isinstance(raw, dict):
        item_payload = raw
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be a JSON object")

    tenant = (payload.get("tenant") or "default").strip() or "default"
    project = (payload.get("project") or "").strip() or None
    try:
        priority = int(payload.get("priority", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "priority must be an integer") from exc

    admission = _examlops_admission()
    item_id = admission.submit(
        kind,
        item_payload,
        tenant=tenant,
        project=project,
        priority=priority,
    )
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "admission_submit",
            kind,
            {"id": item_id, "tenant": tenant, "project": project, "priority": priority},
        )
        conn.commit()
        conn.close()
        return {"id": item_id, "kind": kind, "tenant": tenant, "priority": priority}
    finally:
        conn.close()
