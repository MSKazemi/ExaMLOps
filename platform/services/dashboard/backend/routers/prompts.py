"""Prompt registry (B1, dashboard-rebuild M2).

Reads (viewer): prompt names → their immutable versions + moving labels. Writes (admin +
`prompt.manage`, audited): create a new version (variables auto-declared from the template) and
point/rollback a label — reusing the shared `examlops.data.prompts` + `examlops.prompts` code paths
(pure platform.db, no external service). Mirrors `exa prompt create|label|rollback`.
"""

from __future__ import annotations

import json
import os

import audit_write
from auth import require_role
from capabilities import PROMPT_MANAGE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/prompts", tags=["prompts"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, PROMPT_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, PROMPT_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_prompts():
    """Lazy, guarded import of the shared prompt code paths (503 if unavailable)."""
    try:
        from examlops.data import prompts as _dp  # type: ignore
        from examlops.prompts import declared_variables as _dv  # type: ignore

        return _dp, _dv
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "prompt writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("")
async def list_prompts(_=Depends(_viewer)) -> list[dict]:
    """All prompts with their versions (newest first) + labels. Fail-open to []."""
    try:
        conn = connect(_db_path())
        try:
            vrows = conn.execute(
                "SELECT name, version, variables, actor, created_at FROM prompt_versions "
                "ORDER BY name, version DESC"
            ).fetchall()
            lrows = conn.execute(
                "SELECT name, label, version, updated_at FROM prompt_labels ORDER BY name, label"
            ).fetchall()
            conn.close()
            prompts: dict[str, dict] = {}
            for r in vrows:
                p = prompts.setdefault(r["name"], {"name": r["name"], "versions": [], "labels": []})
                p["versions"].append(
                    {
                        "version": r["version"],
                        "variables": json.loads(r["variables"] or "[]"),
                        "actor": r["actor"],
                        "created_at": r["created_at"],
                    }
                )
            for r in lrows:
                p = prompts.setdefault(r["name"], {"name": r["name"], "versions": [], "labels": []})
                p["labels"].append(
                    {"label": r["label"], "version": r["version"], "updated_at": r["updated_at"]}
                )
            return list(prompts.values())
        finally:
            conn.close()
    except Exception:
        return []


@router.post("/{name}/versions", status_code=status.HTTP_201_CREATED)
async def create_version(
    name: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Create a new immutable prompt version (admin; audited).

    Body: ``{template, label?}``. Variables are auto-declared from the template (`{var}` tokens).
    Mirrors ``exa prompt create`` via the shared `create_prompt_version` (+ optional `set_prompt_label`).
    """
    _require_manage(principal)
    template = payload.get("template")
    if not template or not str(template).strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "template is required")
    label = (payload.get("label") or "").strip()
    dp, declared_variables = _examlops_prompts()
    actor = principal.get("sub", "?")
    variables = declared_variables(template)
    # Do the shared-fn writes first (each opens+commits+closes its own connection), THEN write the
    # audit on the dashboard connection — never hold an uncommitted dashboard write across a shared
    # call, or the two connections deadlock ("database is locked").
    version = dp.create_prompt_version(name, template, variables=variables, actor=actor)
    if label:
        dp.set_prompt_label(name, label, version)
    conn = connect(_db_path())
    try:
        _audit(conn, actor, "prompt_create", name, {"version": version, "variables": variables})
        if label:
            _audit(conn, actor, "prompt_label", name, {"label": label, "version": version})
        conn.commit()
        conn.close()
        return {"name": name, "version": version, "variables": variables, "label": label or None}
    finally:
        conn.close()


@router.post("/{name}/label")
async def set_label(
    name: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Point a label at a version — also serves rollback (admin; audited).

    Body: ``{label, version}``. Mirrors ``exa prompt label`` / ``exa prompt rollback``.
    """
    _require_manage(principal)
    label = (payload.get("label") or "").strip()
    if not label:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "label is required")
    raw_version = payload.get("version")
    if raw_version is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "version must be an integer")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "version must be an integer") from exc
    dp, _dv = _examlops_prompts()
    if dp.get_prompt_version(name, version) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name} v{version} does not exist")
    dp.set_prompt_label(name, label, version)
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "prompt_label",
            name,
            {"label": label, "version": version},
        )
        conn.commit()
        conn.close()
        return {"name": name, "label": label, "version": version}
    finally:
        conn.close()
