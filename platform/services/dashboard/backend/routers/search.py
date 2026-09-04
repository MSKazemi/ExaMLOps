"""Federated global-search BFF endpoint (F2 / ADR 0056).

``GET /api/v1/search?q=`` fans out across models / jobs / audit / pages and returns typed,
grouped, ranked results (each linking to its F1 entity URL). Viewer-gated and composed through
the F8 BFF substrate so a slow/failed source degrades to ``_partial`` instead of a 500.
"""

from __future__ import annotations

import os
from typing import Any

import search as search_lib
from auth import require_role
from bff import aggregate
from fastapi import APIRouter, Depends
from security import RateLimiter, rate_limit

router = APIRouter(prefix="/v1", tags=["search"])
_viewer = require_role("viewer")

# Search fans out across every source per keystroke, so guard it against floods (F16 R7).
_search_limiter = RateLimiter(limit=30, window_seconds=10.0)
_search_rate = rate_limit(_search_limiter)


def _platform_db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


@router.get("/search")
async def global_search(
    q: str = "", limit: int = 20, _=Depends(_viewer), __=Depends(_search_rate)
) -> dict[str, Any]:
    """Federated global search (F2 R3): grouped, ranked, entity-linked results."""
    db = _platform_db_path()

    def _run() -> dict[str, Any]:
        return search_lib.search(db, q, limit=limit)

    return await aggregate({"search": _run})
