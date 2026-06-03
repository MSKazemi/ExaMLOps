"""Audit log read-only router (admin-only)."""

from datetime import datetime

from auth import require_role
from database import get_db
from fastapi import APIRouter, Depends, Query
from models import DashboardAudit
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditRow(BaseModel):
    id: int
    at: datetime
    role: str
    action: str
    key: str


class AuditPage(BaseModel):
    items: list[AuditRow]
    total: int


@router.get(
    "",
    response_model=AuditPage,
    summary="Recent audit rows (admin only)",
    description="Most recent first. limit max 500; default 50.",
)
async def get_audit(
    _: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> AuditPage:
    total = (await db.execute(select(func.count(DashboardAudit.id)))).scalar_one()
    rows = (
        await db.execute(
            select(DashboardAudit)
            .order_by(DashboardAudit.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return AuditPage(
        items=[
            AuditRow(id=r.id, at=r.at, role=r.role, action=r.action, key=r.key)
            for r in rows
        ],
        total=total,
    )
