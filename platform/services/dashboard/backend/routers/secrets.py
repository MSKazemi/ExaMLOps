"""Platform secrets (D7, dashboard-rebuild M3).

Reads (viewer): secret **metadata only** — path, tenant, version, who/when — from `secrets_store`.
The plaintext value is NEVER selected or returned to the browser (hard rule, mirrors the connections
router's `hasSecret`-only contract). Writes (admin + `secrets.manage`, audited): set/update a
secret's value — reusing the shared `examlops.secrets.set_secret` code path (encrypts with the local
Fernet keyring, pure platform.db; no Vault required offline). Mirrors `exa secrets set`. There is
deliberately NO reveal endpoint.
"""

from __future__ import annotations

import os

from auth import require_role
from capabilities import SECRETS_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/secrets", tags=["secrets"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, SECRETS_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, SECRETS_MANAGE))


def _examlops_secrets():
    """Lazy, guarded import of the shared secrets code path (503 if unavailable)."""
    try:
        from examlops import secrets as _s  # type: ignore

        return _s
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "secret writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def list_secrets(_=Depends(_viewer)) -> list[dict]:
    """Secret METADATA only — never the value (path/tenant/version/updated_by/updated_at). Fail-open."""
    try:
        conn = connect(_db_path())
        rows = conn.execute(
            "SELECT path, tenant, version, updated_by, updated_at FROM secrets_store "
            "ORDER BY path, tenant"
        ).fetchall()
        conn.close()
        # `hasValue` is always true for a stored row; the plaintext is intentionally absent.
        return [{**dict(r), "hasValue": True} for r in rows]
    except Exception:
        return []


@router.post("", status_code=status.HTTP_201_CREATED)
async def set_secret(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Set/update a secret's value (admin; audited). The value is write-only — never echoed back.

    Body: ``{path, value, tenant?}``. Mirrors ``exa secrets set`` via the shared
    ``examlops.secrets.set_secret`` (encrypts + audits ``source=dashboard``). The response returns
    the path/tenant/version only — never the value.
    """
    _require_manage(principal)
    path = (payload.get("path") or "").strip()
    value = payload.get("value")
    if not path:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path is required")
    if value is None or value == "":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "value is required")
    tenant = (payload.get("tenant") or "default").strip() or "default"
    s = _examlops_secrets()
    version = s.set_secret(
        path, str(value), tenant=tenant, actor=principal.get("sub", "?"), source="dashboard"
    )
    return {"path": path, "tenant": tenant, "version": version}
