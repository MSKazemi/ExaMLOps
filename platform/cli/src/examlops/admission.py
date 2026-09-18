"""Admission control — fair-share queue between triggers and Prefect (Phase 1 item 1.5).

Every retrain/pipeline trigger (drift, autopilot, the control-plane API, webhooks) submits to a
durable queue instead of dispatching straight to Prefect. A worker drains it under a global
concurrency cap with **per-tenant max-min fairness**, so a fleet-wide drift event — or one noisy
tenant — can't starve everyone else's cluster share. The queue survives a restart; a crashed
worker's in-flight item is reclaimed by TTL.

This module is the thin, dependency-free facade over the ``platform_db`` primitives; the actual
Prefect dispatch is injected (so it's testable offline and the transport can evolve).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw and raw.lstrip("-").isdigit():
        return int(raw)
    return default


def max_running() -> int:
    """Global admission concurrency cap (``EXAMLOPS_ADMISSION_MAX_RUNNING``, default 4)."""
    return max(1, _int_env("EXAMLOPS_ADMISSION_MAX_RUNNING", 4))


def per_tenant_cap() -> int:
    """Per-tenant concurrency cap (``EXAMLOPS_ADMISSION_PER_TENANT``, default 2)."""
    return max(1, _int_env("EXAMLOPS_ADMISSION_PER_TENANT", 2))


def submit(
    kind: str,
    payload: dict[str, Any],
    *,
    tenant: str = "default",
    project: str | None = None,
    priority: int = 0,
) -> int:
    """Enqueue a work item; returns its queue id. Cheap + durable (no dispatch yet)."""
    from examlops.data import init_db
    from examlops.data.admission import enqueue_admission

    init_db()
    return enqueue_admission(kind, payload, tenant=tenant, project=project, priority=priority)


def worker_step(
    dispatch: Callable[[dict[str, Any]], Any],
    *,
    max_running_: int | None = None,
    per_tenant_cap_: int | None = None,
) -> dict[str, Any] | None:
    """Claim the next admissible item (fair-share) and dispatch it. Returns a result dict or None.

    ``dispatch(item)`` performs the real work (POST /retrain, Prefect run, …). On success the item
    is marked ``done``; on exception it's marked ``failed`` with the error — never left dangling.
    """
    from examlops.data import init_db
    from examlops.data.admission import claim_next_admission, complete_admission

    init_db()
    item = claim_next_admission(
        max_running=max_running_ if max_running_ is not None else max_running(),
        per_tenant_cap=per_tenant_cap_ if per_tenant_cap_ is not None else per_tenant_cap(),
    )
    if item is None:
        return None
    try:
        result = dispatch(item)
        complete_admission(item["id"], state="done")
        return {"item": item, "ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001 - one bad item must not kill the worker
        complete_admission(item["id"], state="failed", reason=str(exc))
        return {"item": item, "ok": False, "error": str(exc)}


def drain(dispatch: Callable[[dict[str, Any]], Any], *, max_items: int = 1000) -> dict[str, int]:
    """Repeatedly ``worker_step`` until nothing is admissible (or ``max_items`` reached)."""
    done = failed = 0
    for _ in range(max_items):
        r = worker_step(dispatch)
        if r is None:
            break
        if r["ok"]:
            done += 1
        else:
            failed += 1
    return {"dispatched": done, "failed": failed}


def stats() -> dict[str, Any]:
    """Queue depth by state, plus ``oldest_queued_age_s``.

    **The value space is mixed and that matters to callers.** Five keys are counts; the sixth is a
    duration in seconds, and it is ``None`` when nothing is queued. A consumer that summed
    ``stats().values()`` to get "how many items" therefore raised on an empty queue and, once
    something was waiting, added seconds to an item count — which is exactly what happened to the
    dashboard's admission endpoint. Sum by name, or read `oldest_queued_age_s` separately.

    The annotation said ``dict[str, int]`` until 2026-09-14, which mypy could not catch because the
    helper it delegates to returns ``dict[str, Any]``.
    """
    from examlops.data import init_db
    from examlops.data.admission import admission_stats

    init_db()
    return admission_stats()
