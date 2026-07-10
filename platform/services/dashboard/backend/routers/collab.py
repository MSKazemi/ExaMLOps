"""Collaboration & workflow endpoints (F22 / ADR 0073).

Comments/annotations on entities, an entity activity trail, and shareable time-frozen snapshots behind
scoped, expiring, read-only tokens. Viewer-gated; tenant-scoped (F15); sanitized (F16); audited (D4).
@-mentions publish a notification on the F8 bus (F12).
"""

from __future__ import annotations

import os
from typing import Any

import collab as collab_lib
from auth import require_role
from capabilities import principal_from_claims
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from realtime import bus

router = APIRouter(prefix="/v1/collab", tags=["collaboration"])
_viewer = require_role("viewer")


def _db() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


class CommentBody(BaseModel):
    body: str


class SnapshotBody(BaseModel):
    view: dict[str, Any] = {}
    ttl_hours: int = 168


@router.get("/{entity_type}/{entity_id}/comments")
async def list_comments(entity_type: str, entity_id: str, claims: dict = Depends(_viewer)) -> dict[str, Any]:
    p = principal_from_claims(claims)
    return {"comments": collab_lib.list_comments(_db(), entity_type, entity_id, p["tenant"])}


@router.post("/{entity_type}/{entity_id}/comments")
async def add_comment(
    entity_type: str, entity_id: str, req: CommentBody, claims: dict = Depends(_viewer)
) -> dict[str, Any]:
    p = principal_from_claims(claims)
    comment = collab_lib.add_comment(_db(), entity_type, entity_id, p["tenant"], p["sub"], req.body)
    # Notify each @-mentioned user over the F8 channel (F12).
    for user in comment["mentions"]:
        bus.publish("event.mention", {"user": user, "entity": f"{entity_type}/{entity_id}", "by": p["sub"]})
    return comment


@router.get("/{entity_type}/{entity_id}/activity")
async def activity(entity_type: str, entity_id: str, claims: dict = Depends(_viewer)) -> dict[str, Any]:
    p = principal_from_claims(claims)
    return {"activity": collab_lib.entity_activity(_db(), entity_type, entity_id, p["tenant"])}


@router.post("/snapshot")
async def create_snapshot(req: SnapshotBody, claims: dict = Depends(_viewer)) -> dict[str, Any]:
    p = principal_from_claims(claims)
    return collab_lib.create_snapshot(_db(), p["tenant"], req.view, p["sub"], req.ttl_hours)


@router.get("/snapshot/{token}")
async def resolve_snapshot(token: str, claims: dict = Depends(_viewer)) -> dict[str, Any]:
    snap = collab_lib.get_snapshot(_db(), token)
    if snap is None:
        return {"found": False}
    return {"found": True, **snap}
