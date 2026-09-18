"""Feature store — feature views (A3, dashboard-rebuild M5).

Reads (viewer): registered feature views (one train/serve definition each). Writes (admin +
`feature.manage`, audited): register/patch a feature view — reusing the shared
`examlops.feature_store.apply_view` code path (pure platform.db). Mirrors `exa feature apply`.
"""

from __future__ import annotations

import json

import audit_write
from auth import require_role
from capabilities import FEATURE_MANAGE, can, deny_reason, require_capability
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/feature-store", tags=["feature-store"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _db_path() -> str:
    return platform_db_path()


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, FEATURE_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, FEATURE_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_feature_store():
    """Lazy, guarded import of the shared feature-store code path (503 if unavailable)."""
    try:
        from examlops import feature_store as _fs  # type: ignore

        return _fs
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "feature-store writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("/views")
async def list_views(_=Depends(_viewer)) -> list[dict]:
    """Registered feature views (features parsed from JSON). Fail-open to []."""
    try:
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT name, entity, features_json, source, ttl_seconds, dataset_revision, updated_at "
                "FROM feature_views ORDER BY name"
            ).fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                d["features"] = json.loads(d.pop("features_json") or "[]")
                out.append(d)
            return out
        finally:
            conn.close()
    except Exception:
        return []


@router.post("/views", status_code=status.HTTP_201_CREATED)
async def apply_view(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(FEATURE_MANAGE)),
) -> dict:
    """Register/patch a feature view (admin; audited).

    Body: ``{name, entity, features: string[], source?, ttlSeconds?, datasetRevision?}``. Mirrors
    ``exa feature apply`` via the shared `examlops.feature_store.apply_view` (one definition for
    train + serve).
    """
    _require_manage(principal)
    name = (payload.get("name") or "").strip()
    entity = (payload.get("entity") or "").strip()
    features = payload.get("features")
    if not name or not entity:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name and entity are required")
    if not isinstance(features, list) or not features:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "features must be a non-empty list")
    try:
        ttl = int(payload.get("ttlSeconds", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ttlSeconds must be an integer") from exc
    if ttl < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ttlSeconds must be >= 0")
    fs = _examlops_feature_store()
    fs.apply_view(
        fs.FeatureView(
            name=name,
            entity=entity,
            features=[str(f) for f in features],
            source=(payload.get("source") or None),
            ttl_seconds=ttl,
            dataset_revision=(payload.get("datasetRevision") or None),
        )
    )
    conn = connect(_db_path())
    try:
        _audit(
            conn,
            principal.get("sub", "?"),
            "feature_view_apply",
            name,
            {"entity": entity, "features": features},
        )
        conn.commit()
        conn.close()
        return {"name": name, "entity": entity, "features": features, "ttlSeconds": ttl}
    finally:
        conn.close()
