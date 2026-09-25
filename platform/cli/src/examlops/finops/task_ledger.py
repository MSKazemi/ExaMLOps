"""The per-task cost ledger for agentic work (ADR 0148 decision 4, Verification 4).

An agent task's cost is not token-shaped. It is::

    per task = Σ model calls + Σ tool calls + sandbox-seconds + idle-state GB-hours
               + hot-pool standby share

This module is the one ledger those components are written to, and :func:`task_cost` returns a
total that **equals the sum of its entries by construction** (it is computed from them, never
stored separately). Rules the ADR sets and this module enforces:

* **Every entry is owned by a project.** Hot pools and warm sandbox pools are attributed to the
  owning project, never left as unowned overhead — an entry without a project is refused.
* **Standby is apportioned by a declared rule, published with the number** (the ADR's mitigation
  for double counting shared hot pools). :func:`apportion_standby` supports ``equal`` and
  ``weighted`` (e.g. by GPU-seconds); the shares sum *exactly* to the pool cost, and the rule is
  recorded in each entry's ``method`` and in the audit event.
* **Absent is not zero.** A component with no entries is reported as unmetered for that task, and
  the total is flagged ``complete: False`` until every component has been metered.
* **No invented prices.** Sandbox-seconds and idle-state GB-hours are priced by an explicit rate
  (argument, or ``EXAMLOPS_SANDBOX_USD_PER_SECOND`` / ``EXAMLOPS_IDLE_USD_PER_GB_HOUR``); with no
  rate the call refuses rather than recording 0.
* Entries are telemetry, not decisions: they are not audited one by one (ADR 0148 d7). An
  apportioning *is* a decision and is audited.
"""

from __future__ import annotations

import hashlib
import math
import os
import uuid
from typing import Any

__all__ = [
    "APPORTION_RULES",
    "COMPONENTS",
    "TaskLedgerError",
    "apportion_standby",
    "record_idle_state",
    "record_entry",
    "record_sandbox",
    "task_cost",
]

#: component -> unit it is metered in
COMPONENTS: dict[str, str] = {
    "model_call": "call",
    "tool_call": "call",
    "sandbox_seconds": "second",
    "idle_state_gb_hours": "GB-hour",
    "hot_pool_standby": "share",
}
APPORTION_RULES = ("equal", "weighted")
_MAX_TASKS_PER_APPORTION = 10_000


class TaskLedgerError(ValueError):
    """An entry the ledger refuses (no owner, unknown component, no rate, bad value)."""


def _finite_nonneg(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _rate(explicit: float | None, env: str) -> float:
    if explicit is not None:
        rate = explicit
    else:
        raw = os.getenv(env, "").strip()
        if not raw:
            raise TaskLedgerError(f"no rate: pass one or set {env} (a cost is never assumed 0)")
        try:
            rate = float(raw)
        except ValueError as exc:
            raise TaskLedgerError(f"{env} is not a number: {raw!r}") from exc
    if not _finite_nonneg(rate):
        raise TaskLedgerError(f"rate must be a finite non-negative number, got {rate!r}")
    return float(rate)


def _entry(
    task_id: str,
    component: str,
    *,
    project: str,
    quantity: float,
    cost_usd: float,
    tenant: str,
    method: str | None,
    entry_id: str | None,
) -> dict[str, Any]:
    if not (task_id or "").strip():
        raise TaskLedgerError("a task id is required")
    if not (project or "").strip():
        raise TaskLedgerError("every cost entry is owned by a project (ADR 0148 d4)")
    if component not in COMPONENTS:
        raise TaskLedgerError(
            f"unknown component {component!r}; expected one of {list(COMPONENTS)}"
        )
    if not _finite_nonneg(quantity):
        raise TaskLedgerError(f"quantity must be a finite non-negative number, got {quantity!r}")
    if not _finite_nonneg(cost_usd):
        raise TaskLedgerError(f"cost_usd must be a finite non-negative number, got {cost_usd!r}")
    return {
        "entry_id": entry_id or f"{task_id}:{component}:{uuid.uuid4().hex}",
        "task_id": task_id,
        "project": project,
        "tenant": tenant or "default",
        "component": component,
        "quantity": float(quantity),
        "unit": COMPONENTS[component],
        "cost_usd": float(cost_usd),
        "method": method,
    }


def record_entry(
    task_id: str,
    component: str,
    cost_usd: float,
    *,
    project: str,
    quantity: float = 1.0,
    tenant: str = "default",
    method: str | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Record one metered entry. Pass ``entry_id`` to make a retried write idempotent."""
    from examlops.data import task_costs as store

    row = _entry(
        task_id,
        component,
        project=project,
        quantity=quantity,
        cost_usd=cost_usd,
        tenant=tenant,
        method=method,
        entry_id=entry_id,
    )
    created = store.insert_entries([row])
    return {**row, "created": bool(created)}


def record_sandbox(
    task_id: str,
    seconds: float,
    *,
    project: str,
    usd_per_second: float | None = None,
    tenant: str = "default",
    entry_id: str | None = None,
) -> dict[str, Any]:
    rate = _rate(usd_per_second, "EXAMLOPS_SANDBOX_USD_PER_SECOND")
    if not _finite_nonneg(seconds):
        raise TaskLedgerError(f"seconds must be a finite non-negative number, got {seconds!r}")
    return record_entry(
        task_id,
        "sandbox_seconds",
        seconds * rate,
        project=project,
        quantity=seconds,
        tenant=tenant,
        method=f"sandbox_seconds x {rate:g} USD/s",
        entry_id=entry_id,
    )


def record_idle_state(
    task_id: str,
    gb: float,
    hours: float,
    *,
    project: str,
    usd_per_gb_hour: float | None = None,
    tenant: str = "default",
    entry_id: str | None = None,
) -> dict[str, Any]:
    rate = _rate(usd_per_gb_hour, "EXAMLOPS_IDLE_USD_PER_GB_HOUR")
    if not (_finite_nonneg(gb) and _finite_nonneg(hours)):
        raise TaskLedgerError("gb and hours must be finite non-negative numbers")
    gb_hours = gb * hours
    return record_entry(
        task_id,
        "idle_state_gb_hours",
        gb_hours * rate,
        project=project,
        quantity=gb_hours,
        tenant=tenant,
        method=f"{gb:g} GB x {hours:g} h x {rate:g} USD/GB-h",
        entry_id=entry_id,
    )


def _split(total: float, weights: dict[str, float]) -> dict[str, float]:
    """Largest-remainder split in micro-dollars, so the shares sum *exactly* to ``total``."""
    micros = round(total * 1_000_000)
    wsum = sum(weights.values())
    raw = {k: micros * w / wsum for k, w in weights.items()}
    floors = {k: math.floor(v) for k, v in raw.items()}
    left = micros - sum(floors.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - floors[k]), k))[:left]:
        floors[k] += 1
    return {k: v / 1_000_000 for k, v in floors.items()}


def apportion_standby(
    pool: str,
    pool_cost_usd: float,
    tasks: dict[str, float] | list[str],
    *,
    project: str,
    rule: str = "equal",
    tenant: str = "default",
    period: str = "",
    actor: str | None = None,
) -> dict[str, Any]:
    """Split a hot pool's standby cost across the tasks it served, by a declared rule.

    ``tasks`` is a list of task ids (``equal``) or ``{task_id: weight}`` (``weighted``, e.g.
    GPU-seconds each task used). Idempotent per ``(pool, period, task)``: re-apportioning the
    same period records nothing new.
    """
    from examlops.data import task_costs as store
    from examlops.data.audit import audit_best_effort

    if rule not in APPORTION_RULES:
        raise TaskLedgerError(f"rule must be one of {APPORTION_RULES}, got {rule!r}")
    if not (pool or "").strip():
        raise TaskLedgerError("a pool name is required")
    if not _finite_nonneg(pool_cost_usd):
        raise TaskLedgerError("pool_cost_usd must be a finite non-negative number")
    if isinstance(tasks, dict):
        weights = {str(k): float(v) for k, v in tasks.items()}
    else:
        weights = {str(t): 1.0 for t in tasks}
    if not weights:
        raise TaskLedgerError("no tasks to apportion to: standby would be left unowned")
    if len(weights) > _MAX_TASKS_PER_APPORTION:
        raise TaskLedgerError(f"at most {_MAX_TASKS_PER_APPORTION} tasks per apportioning")
    if rule == "equal":
        weights = dict.fromkeys(weights, 1.0)
    if any(not _finite_nonneg(w) for w in weights.values()) or sum(weights.values()) <= 0:
        raise TaskLedgerError("weights must be non-negative and not all zero")
    shares = _split(float(pool_cost_usd), weights)
    wsum = sum(weights.values())
    method = f"hot_pool_standby:{rule}:{pool}"
    tag = hashlib.sha256(f"{tenant}|{pool}|{period}".encode()).hexdigest()[:16]
    prefix = f"standby:{tag}:"
    rows = [
        _entry(
            task,
            "hot_pool_standby",
            project=project,
            quantity=weights[task] / wsum,
            cost_usd=share,
            tenant=tenant,
            method=method,
            entry_id=f"{prefix}{task}",
        )
        for task, share in shares.items()
    ]
    try:
        # One apportioning per (pool, period): an identical re-run is a no-op, a *different*
        # split is refused as a whole — never merged row by row into the first one.
        created = store.insert_entries(rows, exclusive_prefix=prefix, tenant=tenant)
    except ValueError as exc:
        raise TaskLedgerError(
            f"pool {pool!r} period {period!r} is already apportioned with a different split; "
            "use a new --period for a new apportioning"
        ) from exc
    if created:
        audit_best_effort(
            "finops",
            actor,
            "hot_pool_standby_apportioned",
            pool,
            {
                "rule": rule,
                "pool_cost_usd": pool_cost_usd,
                "tasks": len(rows),
                "period": period,
                "project": project,
            },
            tenant=tenant,
        )
    return {
        "pool": pool,
        "rule": rule,
        "period": period,
        "project": project,
        "pool_cost_usd": float(pool_cost_usd),
        "shares": shares,
        "created": created,
        "sum_usd": round(sum(shares.values()), 6),
    }


def task_cost(task_id: str, *, tenant: str = "default") -> dict[str, Any]:
    """The task's ledger: per-component totals, the total, and which components were metered."""
    from examlops.data import task_costs as store

    # Totals are SQL aggregates over every entry; ``rows`` is a bounded page for display. Summing
    # the page would silently drop everything past the cap from the total.
    totals = store.task_totals(task_id, tenant=tenant)
    rows = store.task_entries(task_id, tenant=tenant)
    by: dict[str, float] = totals["components"]
    unmetered = [c for c in COMPONENTS if c not in by]
    n = int(totals["entries"])
    return {
        "task_id": task_id,
        "tenant": tenant,
        "entries": n,
        "components": {c: round(v, 9) for c, v in by.items()},
        "total_usd": round(sum(by.values()), 9),
        "unmetered": unmetered,
        "complete": n > 0 and not unmetered,
        "projects": totals["projects"],
        "rows": rows,
        "rows_truncated": len(rows) < n,
    }
