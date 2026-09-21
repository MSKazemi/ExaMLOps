"""Two-phase quota reservation service (ADR 0116 decision 3).

Storage and atomicity live in :mod:`examlops.data.quota_reservations`. This module decides *which
limits* a reservation is checked against, and audits every transition through
``audit_best_effort`` (a lost record is counted, never silent).

* GPU concurrency limit = the project's ``gpu_limit`` (``exa project create --gpu-limit``); a
  project with none (stored as 0) is not limited on that axis.
* GPU-hour headroom = the project budget's ``gpu_hours_budget`` minus the spend already recorded
  in the budget's period, when the request carries ``est_runtime_s``. The *held* hours are then
  subtracted inside the atomic reserve, so two reservations cannot both fit the last hours.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from examlops.data import quota_reservations as store
from examlops.data.audit import audit_best_effort

from .request import JobRequest

_SOURCE = "admission-seam"
TTL_ENV = "EXAMLOPS_RESERVATION_TTL_S"
DEFAULT_TTL_S = 900.0


def default_ttl_s() -> float:
    try:
        return max(1.0, float(os.getenv(TTL_ENV, str(DEFAULT_TTL_S))))
    except ValueError:
        return DEFAULT_TTL_S


def _audit(action: str, target: str, details: dict[str, Any], tenant: str, actor: str | None):
    audit_best_effort(_SOURCE, actor, action, target, details, tenant=tenant)


def _limits(request: JobRequest) -> tuple[int | None, float | None]:
    from examlops.data.projects import get_project
    from examlops.project_finops import budget_status

    proj = get_project(request.project)
    # `gpu_limit` is NOT NULL DEFAULT 0 and 0 is what a project created without a GPU quota
    # holds, so 0 reads as "no concurrency quota declared", not "zero GPUs allowed".
    gpus_limit = proj.get("gpu_limit") if proj else None
    if gpus_limit is not None and int(gpus_limit) <= 0:
        gpus_limit = None
    hours_limit: float | None = None
    if request.est_runtime_s is not None:
        status = budget_status(request.project)
        budget = status.get("budget") or {}
        if budget.get("gpu_hours_budget") is not None:
            hours_limit = max(
                0.0, float(budget["gpu_hours_budget"]) - status["consumption"]["gpu_hours"]
            )
    return (int(gpus_limit) if gpus_limit is not None else None), hours_limit


def reserve(
    request: JobRequest,
    *,
    ttl_s: float | None = None,
    holder: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Reserve ``request``'s GPUs / GPU-hours. ``{"ok", "reservation"|"reason"}``."""
    request.validate()
    gpus_limit, hours_limit = _limits(request)
    rid = uuid.uuid4().hex
    out = store.reserve(
        rid,
        request.project,
        tenant=request.tenant,
        gpus=request.resources.gpus,
        gpu_hours=request.gpu_hours,
        ttl_s=default_ttl_s() if ttl_s is None else ttl_s,
        gpus_limit=gpus_limit,
        gpu_hours_limit=hours_limit,
        holder=holder,
    )
    _audit(
        "quota_reserved" if out["ok"] else "quota_reservation_refused",
        request.project,
        {"id": rid, "gpus": request.resources.gpus, "gpu_hours": request.gpu_hours}
        | ({} if out["ok"] else {"reason": out["reason"]}),
        request.tenant,
        actor,
    )
    return out


def _transition(name: str, rid: str, ok: bool, actor: str | None, **extra: Any) -> bool:
    row = store.get(rid)
    if ok and row:
        _audit(name, row["project"], {"id": rid, **extra}, row["tenant"], actor)
    return ok


def commit(rid: str, *, actor: str | None = None) -> bool:
    return _transition("quota_committed", rid, store.commit(rid), actor)


def release(rid: str, *, reason: str | None = None, actor: str | None = None) -> bool:
    return _transition(
        "quota_released", rid, store.release(rid, reason=reason), actor, reason=reason
    )


def expire(*, dry_run: bool = False, actor: str | None = None) -> list[dict[str, Any]]:
    """Sweep lapsed reservations (or, with ``dry_run``, only list them)."""
    rows = store.expire_due(dry_run=dry_run)
    if not dry_run:
        for r in rows:
            _audit("quota_reservation_expired", r["project"], {"id": r["id"]}, r["tenant"], actor)
    return rows
