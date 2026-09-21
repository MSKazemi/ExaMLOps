"""Embedded copilot endpoint (F11 / ADR 0065).

`POST /api/v1/copilot/ask` proxies a grounded question to the existing Skipper agent bridge and returns
an answer plus **propose-only** `exa` action suggestions (never executed here — R5) and an agent trace
(R6). Viewer-gated; every query is audited to `platform_db` (D4). There is deliberately **no** execution
endpoint: proposals route through the existing authorized/approval/audited action flow.
"""

from __future__ import annotations

import uuid
from typing import Any

import copilot as copilot_lib
from auth import require_role
from dbconn import platform_db_path
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from settings import settings

router = APIRouter(prefix="/v1/copilot", tags=["copilot"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return platform_db_path()


class CopilotContext(BaseModel):
    page: str = "unknown"
    entity: dict[str, Any] | None = None
    filters: dict[str, Any] | None = None


class AskRequest(BaseModel):
    question: str
    context: CopilotContext | None = None


@router.post("/ask")
async def ask(req: AskRequest, claims: dict = Depends(_viewer)) -> dict[str, Any]:
    """Answer a grounded question; return propose-only actions + trace (F11 R4/R5/R6)."""
    question = (req.question or "").strip()
    if not question:
        return {"answer": "", "hitl_required": False, "proposals": [], "trace": []}

    ctx = req.context.model_dump() if req.context else None
    result = await copilot_lib.ask_copilot(
        question,
        ctx,
        agent_url=settings.agent_url,
        token=settings.copilot_agent_api_key,
        timeout=settings.copilot_timeout_s,
        # Never trust a browser-supplied checkpoint key. The signed login id isolates users while
        # preserving multi-turn context. Legacy tokens without a jti get a fresh, safe thread.
        session=f"dashboard-copilot-{claims.get('jti') or uuid.uuid4().hex}",
    )
    actor = claims.get("role", "unknown")
    copilot_lib.audit_copilot(
        _platform_db_path(), actor, question, ctx, result.get("proposals", [])
    )
    return result
