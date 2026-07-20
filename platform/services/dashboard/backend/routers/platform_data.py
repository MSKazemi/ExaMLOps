"""Traffic rules and promotion rules from shared platform.db.

Reads (viewer): traffic split + promotion rules. Writes (admin, audited): set a model's traffic
split across aliases — the same operation as ``exa serve traffic``, reusing
``examlops.data.serving.set_traffic_rules`` so the dashboard can't drift from the CLI.
"""

from __future__ import annotations

import json
import os

from auth import require_role
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/platform-data", tags=["platform-data"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, target, json.dumps(details)),
    )


@router.get("/traffic-rules")
async def get_all_traffic_rules(_=Depends(_viewer)) -> list[dict]:
    """All traffic split rules."""
    try:
        conn = connect(_db_path())
        rows = conn.execute(
            "SELECT model, rules, updated_at, updated_by FROM traffic_rules ORDER BY model"
        ).fetchall()
        conn.close()
        return [
            {
                "model": r["model"],
                "rules": json.loads(r["rules"]),
                "updated_at": r["updated_at"],
                "updated_by": r["updated_by"],
            }
            for r in rows
        ]
    except Exception:
        return []


@router.get("/traffic-rules/{model}")
async def get_model_traffic_rules(model: str, _=Depends(_viewer)) -> dict | None:
    """Traffic split rules for one model."""
    try:
        conn = connect(_db_path())
        row = conn.execute(
            "SELECT model, rules, updated_at, updated_by FROM traffic_rules WHERE model=?", (model,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "model": row["model"],
            "rules": json.loads(row["rules"]),
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }
    except Exception:
        return None


@router.get("/promotion-rules")
async def get_all_promotion_rules(_=Depends(_viewer)) -> list[dict]:
    """All metric-gated promotion rules."""
    try:
        conn = connect(_db_path())
        rows = conn.execute("SELECT * FROM promotion_rules ORDER BY model").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _examlops_serving():
    """Lazy, guarded import of the shared CLI serving code path (503 if unavailable)."""
    try:
        from examlops.data import serving as _s  # type: ignore

        return _s
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "traffic writes require the examlops package (not available in this deployment)",
        ) from exc


@router.put("/traffic-rules/{model}")
async def set_model_traffic_rules(
    model: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Set a model's traffic split across aliases (admin; audited).

    Body: ``{rules: {alias: percent, ...}}`` — integer percents that must sum to 100 (mirrors
    ``exa serve traffic``). Reuses ``examlops.data.serving.set_traffic_rules``.
    """
    rules = payload.get("rules")
    if not isinstance(rules, dict) or not rules:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rules must be a non-empty object")
    clean: dict[str, int] = {}
    for alias, pct in rules.items():
        try:
            ipct = int(pct)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"weight for '{alias}' must be an integer"
            ) from exc
        if ipct < 0 or ipct > 100:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"weight for '{alias}' must be between 0 and 100"
            )
        if ipct:  # drop zero-weight aliases, matching CLI behaviour
            clean[str(alias)] = ipct
    total = sum(clean.values())
    if total != 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"weights must sum to 100 (got {total})")
    serving = _examlops_serving()
    actor = principal.get("sub", "?")
    serving.set_traffic_rules(model, clean, updated_by=actor)
    conn = connect(_db_path())
    _audit(conn, actor, "traffic_rules_set", model, {"rules": clean})
    conn.commit()
    conn.close()
    return {"model": model, "rules": clean, "updatedBy": actor}
