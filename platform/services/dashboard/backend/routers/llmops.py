"""LLMOps console BFF endpoint (F10 / ADR 0064).

`GET /api/v1/llmops/overview` composes the LLM endpoint registry + continuous-eval scores through
the F8 BFF substrate. Viewer-gated, partial-failure safe; unavailable backends degrade to empty
sections (F10 R6).
"""

from __future__ import annotations

import os
from typing import Any

import llmops
from auth import require_role
from bff import aggregate
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/v1/llmops", tags=["llmops"])
_viewer = require_role("viewer")


def _platform_db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/overview")
async def overview(_=Depends(_viewer)) -> dict[str, Any]:
    """LLMOps overview: endpoint registry + eval scores (F10 R1/R2)."""
    db = _platform_db_path()

    async def _wrap(fn) -> Any:
        return fn(db)

    return await aggregate(
        {
            "endpoints": lambda: _wrap(llmops.endpoints),
            "evals": lambda: _wrap(llmops.eval_summary),
        }
    )
