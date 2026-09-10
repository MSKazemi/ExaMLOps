"""Site feature profile — which modules this centre runs (ADR 0128).

``GET /api/v1/modules`` returns the effective profile and every module's state, so the UI can
hide what the site switched off. Viewer-readable: it names features, not secrets. Changing the
profile stays a CLI/CLI-Console action (``exa modules enable|disable``) — one write path, audited.
"""

from __future__ import annotations

from typing import Any

from auth import require_role
from fastapi import APIRouter, Depends
from module_gate import current_profile

router = APIRouter(prefix="/v1/modules", tags=["modules"])

_viewer = require_role("viewer")


@router.get("")
async def modules(_claims: dict = Depends(_viewer)) -> dict[str, Any]:
    from examlops.lifecycle.modules import CATALOG

    profile = current_profile()
    if profile is None:
        return {"available": False, "modules": []}
    return {
        "available": True,
        "preset": profile.preset,
        "spec": profile.spec(),
        "sources": profile.sources,
        "warnings": profile.warnings,
        "modules": [
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "enabled": profile.is_enabled(m.id),
                "why": profile.reasons[m.id],
                "flags": list(m.dashboard_flags),
                "api": list(m.dashboard_api),
            }
            for m in CATALOG
        ],
    }
