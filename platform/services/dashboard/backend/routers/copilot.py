"""Embedded copilot endpoint (F11 / ADR 0065).

`POST /api/v1/copilot/ask` proxies a grounded question to the existing Skipper agent bridge and returns
an answer plus **propose-only** `exa` action suggestions (never executed here — R5) and an agent trace
(R6). Viewer-gated; every query is audited to `platform_db` (D4). There is deliberately **no** execution
endpoint: proposals route through the existing authorized/approval/audited action flow.
"""

from __future__ import annotations

import os
from typing import Any

import copilot as copilot_lib
from auth import require_role
from fastapi import APIRouter, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/v1/copilot", tags=["copilot"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


class CopilotContext(BaseModel):
    page: str = "unknown"
    entity: dict[str, Any] | None = None
    filters: dict[str, Any] | None = None


class AskRequest(BaseModel):
    question: str
    context: CopilotContext | None = None
    session: str = "dashboard-copilot"


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
        agent_url=os.getenv("AGENT_URL", "http://localhost:18004"),
        token=os.getenv("AGENT_API_KEY", ""),
        session=req.session,
    )
    actor = claims.get("role", "unknown")
    copilot_lib.audit_copilot(
        _platform_db_path(), actor, question, ctx, result.get("proposals", [])
    )
    return result
