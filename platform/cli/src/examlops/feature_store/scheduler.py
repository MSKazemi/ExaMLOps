"""Scheduled materialization + freshness monitoring (ADR 0017 clause 4).

A view opts in with ``materialize_interval_seconds > 0`` (in its pack definition or via
``exa feature apply --interval``). :func:`run_materialization_cycle` materializes every view whose
last materialization is older than its interval — callable from anywhere that ticks:

* the control plane's background thread (``CONTROL_PLANE_FEATURE_MATERIALIZE_SECONDS``), which is
  the scheduled path in a running deployment;
* ``exa feature materialize-due`` from cron / a Prefect deployment / by hand.

Every replica may tick: a per-view coordinator lock (``examlops.coordination``) makes one of them
do the work and the others skip it, and the due check is re-read under the lock so a view the
winner just materialized is not done twice. Each materialization writes a
``feature_view_materialized`` audit event.

:func:`freshness_report` is the monitoring surface: age, TTL, staleness and whether a scheduled
view is overdue, for every view — what ``exa feature status`` prints and what the control plane
publishes as ``examlops_feature_view_*`` gauges.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Most views materialized in one cycle — bounds one tick's work; the rest are due next tick.
DEFAULT_MAX_VIEWS_PER_CYCLE = 50


@dataclass
class ViewFreshness:
    view: str
    materialized_at: str | None
    age_seconds: float | None
    ttl_seconds: int
    interval_seconds: int
    stale: bool
    due: bool
    rows: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_ts(now: datetime | None) -> float:
    return (now or datetime.now(UTC)).timestamp()


def _age(materialized_at: str | None, now_ts: float) -> float | None:
    """Seconds since ``materialized_at``. The store stamps ``CURRENT_TIMESTAMP``, which is UTC
    without a zone, so the stamp is read as UTC — never as the host's local time."""
    if not materialized_at:
        return None
    try:
        naive = datetime.strptime(str(materialized_at)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return max(0.0, now_ts - naive.replace(tzinfo=UTC).timestamp())


def view_freshness(view: dict[str, Any], *, now: datetime | None = None) -> ViewFreshness:
    """Freshness of one registry row (as returned by ``list_feature_views``)."""
    from examlops import data as platform_db

    name = str(view["name"])
    ttl = int(view.get("ttl_seconds") or 0)
    interval = int(view.get("materialize_interval_seconds") or 0)
    last = platform_db.last_materialization(name)
    mat_at = str(last["materialized_at"]) if last and last.get("materialized_at") else None
    age = _age(mat_at, _now_ts(now))
    if mat_at is None:
        return ViewFreshness(name, None, None, ttl, interval, stale=True, due=interval > 0)
    stale = ttl > 0 and age is not None and age > ttl
    due = interval > 0 and (age is None or age >= interval)
    rows = int(last["rows"]) if last and last.get("rows") is not None else None
    return ViewFreshness(name, mat_at, age, ttl, interval, stale=stale, due=due, rows=rows)


def freshness_report(*, now: datetime | None = None) -> list[ViewFreshness]:
    """Freshness of every registered view, sorted by name."""
    from examlops import data as platform_db

    return [view_freshness(v, now=now) for v in platform_db.list_feature_views()]


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_materialization_cycle(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    view: str | None = None,
    max_views: int | None = None,
    actor: str | None = None,
    coordinator: Any = None,
) -> dict[str, Any]:
    """Materialize every due view once.

    Returns ``{"due", "materialized", "skipped", "failed", "mirror_failed"}``; ``mirror_failed``
    lists views that materialized durably but whose serving-tier mirror failed.

    ``view`` restricts the cycle to one view (it still only runs if due). ``dry_run`` reports what
    is due without touching anything. A failure on one view is recorded and the cycle continues —
    one broken source must not starve every other view of its schedule.
    """
    from examlops.feature_store import materialize_with_index

    cap = max_views or int(
        os.getenv("EXAMLOPS_FEATURE_MATERIALIZE_MAX_VIEWS", str(DEFAULT_MAX_VIEWS_PER_CYCLE))
    )
    report = [f for f in freshness_report(now=now) if view is None or f.view == view]
    due = [f for f in report if f.due]
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "due": [f.view for f in due],
        "materialized": [],
        "skipped": [],
        "failed": [],
        # Materialized durably, but the serving tier (Redis) did not take the rows.
        "mirror_failed": [],
    }
    if dry_run:
        return out
    if coordinator is None:
        try:
            from examlops.coordination import get_coordinator

            coordinator = get_coordinator()
        except Exception as exc:  # noqa: BLE001 - no coordinator: run uncoordinated, say so
            logger.warning("feature materializer running without a coordinator: %s", exc)
    holder = _holder()
    for f in due[:cap]:
        key = f"feature-materialize:{f.view}"
        ttl = float(max(60, min(f.interval_seconds or 60, 3600)))
        if coordinator is not None and not coordinator.try_lock(key, holder, ttl):
            out["skipped"].append({"view": f.view, "reason": "another replica holds the lock"})
            continue
        try:
            from examlops import data as platform_db

            row = platform_db.get_feature_view(f.view)
            fresh = view_freshness(row, now=now) if row else None
            if fresh is None or not fresh.due:  # done by someone else since we listed it
                out["skipped"].append({"view": f.view, "reason": "no longer due"})
                continue
            result = materialize_with_index(f.view)
            mirror = result.get("online")
            entry = {
                "view": f.view,
                "rows": result["rows"],
                "online_error": getattr(mirror, "error", None),
            }
            out["materialized"].append(entry)
            if entry["online_error"]:
                out["mirror_failed"].append({"view": f.view, "error": entry["online_error"]})
            _audit(f.view, entry, actor)
        except Exception as exc:  # noqa: BLE001 - one view's failure must not stop the cycle
            out["failed"].append({"view": f.view, "error": f"{type(exc).__name__}: {exc}"})
            logger.warning("scheduled materialization of %s failed: %s", f.view, exc)
        finally:
            if coordinator is not None:
                try:
                    coordinator.unlock(key, holder)
                except Exception:  # noqa: BLE001 - the lock expires on its own
                    pass
    for f in due[cap:]:
        out["skipped"].append({"view": f.view, "reason": f"cycle cap {cap} reached"})
    return out


def _audit(view: str, entry: dict[str, Any], actor: str | None) -> None:
    try:
        from examlops.data.audit import audit_best_effort

        audit_best_effort(
            "feature-store",
            actor or os.getenv("EXAMLOPS_ACTOR") or "feature-scheduler",
            "feature_view_materialized",
            view,
            {**entry, "trigger": "schedule"},
        )
    except Exception as exc:  # noqa: BLE001 - audit_best_effort counts a loss itself
        logger.warning("audit for materialization of %s skipped: %s", view, exc)


__all__ = [
    "ViewFreshness",
    "freshness_report",
    "run_materialization_cycle",
    "view_freshness",
]
