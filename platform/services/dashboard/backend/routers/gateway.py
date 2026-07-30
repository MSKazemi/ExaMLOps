"""LLM gateway virtual keys (B2, dashboard-rebuild M2).

Reads (viewer): the `virtual_keys` register — only stored fields (key HASH, scope, budget, spend,
revoked); the raw key is never stored and never re-fetchable. Writes (admin + `gateway.manage`,
audited): issue a key (returns the raw key ONCE) and revoke one — reusing the shared
`examlops.gateway.issue_virtual_key` / `examlops.data.governance.revoke_virtual_key` code paths so the
dashboard can't drift from the CLI. Pure platform.db — no live gateway/LLM runtime required.
"""

from __future__ import annotations

import json
import os

from auth import require_role
from capabilities import GATEWAY_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/gateway", tags=["gateway"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, GATEWAY_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, GATEWAY_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, target, json.dumps(details)),
    )


def _examlops_gateway():
    """Lazy, guarded import of the shared gateway code paths (503 if unavailable)."""
    try:
        from examlops import gateway as _g  # type: ignore
        from examlops.data import governance as _gov  # type: ignore

        return _g, _gov
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "gateway writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("/keys")
async def list_keys(_=Depends(_viewer)) -> list[dict]:
    """Virtual keys — stored fields only (hash, scope, budget, spend, revoked). Never the raw key."""
    try:
        conn = connect(_db_path())
        rows = conn.execute(
            "SELECT key_hash, tenant, project, models_json, budget_usd, spent_usd, created_by, "
            "created_at, revoked FROM virtual_keys ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["models"] = json.loads(d.pop("models_json") or "[]")
            d["revoked"] = bool(d["revoked"])
            out.append(d)
        return out
    except Exception:
        return []


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def issue_key(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Issue a virtual key (admin; audited). Returns the raw key **once** — it is never stored.

    Body: ``{tenant?, project?, models?: string[], budgetUsd?: number}``. Mirrors
    ``exa gateway key issue`` via the shared ``examlops.gateway.issue_virtual_key`` (audits
    ``source=dashboard``). The raw key is returned here and nowhere else — only its SHA-256 hash is
    persisted, so the UI must surface it once and never re-fetch it.
    """
    _require_manage(principal)
    tenant = (payload.get("tenant") or "default").strip() or "default"
    project = (payload.get("project") or "default").strip() or "default"
    models = payload.get("models") or None
    if models is not None and not isinstance(models, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "models must be a list of model names")
    budget = payload.get("budgetUsd")
    budget_usd = float(budget) if budget not in (None, "") else None
    gateway, _gov = _examlops_gateway()
    raw = gateway.issue_virtual_key(
        tenant, project, models, budget_usd, principal.get("sub", "?"), source="dashboard"
    )
    return {
        "key": raw,  # shown ONCE — never stored, never re-fetchable
        "tenant": tenant,
        "project": project,
        "models": models or [],
        "budgetUsd": budget_usd,
    }


@router.post("/keys/{key_hash}/revoke")
async def revoke_key(
    key_hash: str,
    principal: dict = Depends(_admin),
) -> dict:
    """Revoke a virtual key by its hash (admin; audited). Mirrors ``exa gateway key revoke``."""
    _require_manage(principal)
    _gateway, gov = _examlops_gateway()
    gov.revoke_virtual_key(key_hash)
    conn = connect(_db_path())
    _audit(conn, principal.get("sub", "?"), "virtual_key_revoked", key_hash, {})
    conn.commit()
    conn.close()
    return {"keyHash": key_hash, "revoked": True}
