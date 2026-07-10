"""Approvals router — proxies to Control Plane approval endpoints."""

from __future__ import annotations

import logging

import httpx
from auth import require_role
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from realtime import bus
from settings import settings
from sqlalchemy.ext.asyncio import AsyncSession

from routers.config import get_decrypted_secret

log = logging.getLogger("dashboard.approvals")

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _as_dict(value: object) -> dict:
    """Control-plane responses are usually dicts; guard the ``**spread`` for anything else."""
    return value if isinstance(value, dict) else {"result": value}


async def _get_control_plane_token(db: AsyncSession) -> str | None:
    """Return the plaintext control_plane_token from the encrypted config store, or None."""
    return await get_decrypted_secret(db, "control_plane_token")


class RejectBody(BaseModel):
    reason: str


@router.get(
    "",
    summary="List approvals from the Control Plane",
    description="Proxies GET /approvals to the Control Plane. No auth required for reads.",
)
async def list_approvals(
    status: str | None = Query(None, description="Filter by status, e.g. 'pending'"),
) -> list[dict]:
    url = f"{settings.control_plane_url}/approvals"
    params: dict[str, str] = {}
    if status is not None:
        params["status"] = status

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Control Plane unavailable: {exc}"
            ) from exc

    if not resp.is_success:
        # Don't forward the raw upstream body to the client — surface a generic
        # message and log the detail server-side instead of leaking internals.
        log.warning(
            "Control Plane /approvals returned %s: %s", resp.status_code, resp.text[:500]
        )
        raise HTTPException(status_code=resp.status_code, detail="Control Plane returned an error")
    return resp.json()


@router.post(
    "/approve/{model_id}",
    summary="Approve a pending model retrain (admin only)",
)
async def approve_model(
    model_id: str,
    _claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token = await _get_control_plane_token(db)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{settings.control_plane_url}/approve/{model_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Control Plane unavailable: {exc}"
            ) from exc

    if not resp.is_success:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    result = resp.json()
    # Push a live event onto the realtime gateway (F8) so open dashboards update without polling.
    bus.publish("approval.approved", {"model": model_id, **_as_dict(result)})
    return result


@router.post(
    "/reject/{model_id}",
    summary="Reject a pending model retrain (admin only)",
)
async def reject_model(
    model_id: str,
    body: RejectBody,
    _claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token = await _get_control_plane_token(db)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{settings.control_plane_url}/reject/{model_id}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, headers=headers, json={"reason": body.reason})
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Control Plane unavailable: {exc}"
            ) from exc

    if not resp.is_success:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    result = resp.json()
    bus.publish("approval.rejected", {"model": model_id, "reason": body.reason, **_as_dict(result)})
    return result
