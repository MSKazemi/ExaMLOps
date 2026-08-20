"""Providers editor (ADR 0074) — dashboard surface over notebook/CLI-authored calculation providers.

Lets an admin view/edit/upload the Python behind a project's calculations (FinOps cost/carbon, drift,
…) and pick which provider each domain uses — the GUI half of the authored-providers feature (the
notebook API is the other half). Every mutation reuses the same ``examlops.providers`` authoring code
path as ``exa providers`` (so the dashboard can't drift from the CLI), goes through the **AST sandbox**
(uploaded Python that imports/execs/opens is rejected before it lands), is ``providers.manage``-gated,
and is audited. Reads are viewer-gated; source is returned so the editor can show it, but it has
already passed the gate on the way in.
"""

from __future__ import annotations

import json
import os
import sqlite3

from auth import require_role
from capabilities import PROVIDERS_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/v1/providers", tags=["providers"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _audit(actor: str, action: str, target: str, details: dict) -> None:
    try:
        conn = connect(_db_path())
        try:
            conn.execute(
                "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
                ("dashboard", actor, action, target, json.dumps(details)),
            )
            conn.commit()
            conn.close()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # auditing must never block the operation


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, PROVIDERS_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, PROVIDERS_MANAGE))


def _examlops_providers():
    """Lazy, guarded import of the shared authoring code path (503 if the package is unavailable)."""
    try:
        from examlops import providers as _p  # type: ignore

        return _p
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "provider editing requires the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def list_providers_view(
    project: str = Query(...),
    _=Depends(_viewer),
) -> list[dict]:
    """List a project's authored providers (domain/name/status/active). Viewer."""
    try:
        p = _examlops_providers()
        return p.list_project_providers(project)
    except HTTPException:
        raise
    except Exception:
        return []


@router.get("/{project}/{domain}/{name}")
async def read_provider_view(project: str, domain: str, name: str, _=Depends(_viewer)) -> dict:
    """Return the stored source of one authored provider. Viewer."""
    p = _examlops_providers()
    try:
        code = p.read_provider_source(project, domain, name)
    except p.ProviderError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"project": project, "domain": domain, "name": name, "code": code}


@router.post("/validate")
async def validate_provider_view(
    payload: dict = Body(...), principal: dict = Depends(_admin)
) -> dict:
    """Gate-check provider source without saving (admin / providers.manage). Returns ok + any error."""
    _require_manage(principal)
    code = payload.get("code") or ""
    p = _examlops_providers()
    try:
        cls = p.compile_provider(code)
    except p.ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "class": cls.__name__}


@router.post("", status_code=status.HTTP_201_CREATED)
async def save_provider_view(payload: dict = Body(...), principal: dict = Depends(_admin)) -> dict:
    """Create/replace an authored provider (admin / providers.manage; AST-sandboxed; audited).

    Body: ``{project, domain, name, code, activate?}``. The source passes the AST gate before it is
    written — unsafe or invalid code is rejected 400 and never reaches disk.
    """
    _require_manage(principal)
    project = (payload.get("project") or "").strip()
    domain = (payload.get("domain") or "").strip()
    name = (payload.get("name") or "").strip()
    code = payload.get("code") or ""
    if not (project and domain and name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project, domain and name are required")
    p = _examlops_providers()
    actor = principal.get("sub", "?")
    try:
        info = p.save_provider(domain, name, code, project=project, actor=actor)
        if payload.get("activate"):
            p.set_active_provider(project, domain, name)
    except p.ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    _audit(
        actor,
        "provider_authored",
        f"{project}/{domain}/{name}",
        {"class": info["class"], "activated": bool(payload.get("activate"))},
    )
    return {
        "project": project,
        "domain": domain,
        "name": name,
        "class": info["class"],
        "activated": bool(payload.get("activate")),
    }


@router.post("/{project}/{domain}/{name}/activate")
async def activate_provider_view(
    project: str, domain: str, name: str, principal: dict = Depends(_admin)
) -> dict:
    """Make a provider the active one for its ``(project, domain)`` (admin / providers.manage; audited)."""
    _require_manage(principal)
    p = _examlops_providers()
    try:
        p.set_active_provider(project, domain, name)
    except p.ProviderError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    _audit(principal.get("sub", "?"), "provider_activated", f"{project}/{domain}/{name}", {})
    return {"project": project, "domain": domain, "name": name, "active": True}


@router.delete("/{project}/{domain}/{name}")
async def delete_provider_view(
    project: str, domain: str, name: str, principal: dict = Depends(_admin)
) -> dict:
    """Delete an authored provider (admin / providers.manage; audited)."""
    _require_manage(principal)
    p = _examlops_providers()
    if not p.delete_provider(project, domain, name):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"provider '{name}' (domain '{domain}') not found in project '{project}'",
        )
    _audit(principal.get("sub", "?"), "provider_removed", f"{project}/{domain}/{name}", {})
    return {"project": project, "domain": domain, "name": name, "deleted": True}
