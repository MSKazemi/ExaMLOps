"""Scheduled audit-trail maintenance (ADR 0028 decisions 2, 3 and 4).

Until this module the *periodic* half of ADR 0028 was a callable nobody called: the WORM export,
the signed checkpoint and the retention prune were all correct, and all waited for an operator to
remember a cron line. This is the job that runs them:

1. **checkpoint + anchor** — :func:`examlops.audit_worm.checkpoint_and_anchor` with
   ``skip_if_unchanged``: a head that already has an anchored (and, when configured,
   transparency-logged) checkpoint costs one read;
2. **transparency log** — the same call logs a new checkpoint to Rekor / Sigstore when
   configured (:mod:`examlops.audit_transparency`);
3. **retention prune** — only when an operator opted in with ``EXAMLOPS_AUDIT_PRUNE_SCHEDULED=1``
   *and* set ``EXAMLOPS_AUDIT_RETENTION_DAYS``; the prune keeps every gate
   :func:`examlops.data.audit_retention.execute_prune` enforces (verified chain, anchored
   checkpoint and cut, archive written first, signed prune record, itself audited). A refused prune
   is reported, never forced.

One cycle at a time across every process and host: the cycle holds the coordinator lease
``audit-maintenance`` (:mod:`examlops.coordination`). Each cycle's outcome is kept in
``audit_maintenance_runs`` (bounded to the newest :data:`MAX_RUNS_KEPT` rows) so ``exa audit
maintenance-runs`` shows whether the schedule is actually running. The runner is the control
plane (a background thread, ``EXAMLOPS_AUDIT_MAINTENANCE_SECONDS``) or ``exa audit maintain`` for
hosts without one.

A cycle deliberately writes **no** audit event of its own for the checkpoint: an event per cycle
would move the head every time, so every cycle would sign a new checkpoint about its own event and
"unchanged" would never happen. The checkpoint row, the WORM entry and the transparency receipt are
the record. A prune *is* audited (``audit_pruned``) by the retention module.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEASE_KEY = "audit-maintenance"
DEFAULT_INTERVAL_S = 3600.0
MIN_INTERVAL_S = 30.0
MAX_RUNS_KEPT = 1000

#: Failed steps this process saw (the Prometheus counter reads it through the control plane).
_STEP_FAILURES = {"count": 0}
_LAST: dict[str, Any] = {}


def step_failures() -> int:
    return int(_STEP_FAILURES["count"])


def last_result() -> dict[str, Any]:
    return dict(_LAST)


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def interval_seconds() -> float:
    """``EXAMLOPS_AUDIT_MAINTENANCE_SECONDS``: 0 disables the schedule; otherwise >= 30 s."""
    raw = os.getenv("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        logger.error("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS=%r is not a number - using default", raw)
        return DEFAULT_INTERVAL_S
    if value <= 0:
        return 0.0
    return max(MIN_INTERVAL_S, value)


def lease_ttl(interval: float) -> float:
    raw = os.getenv("EXAMLOPS_AUDIT_MAINTENANCE_LEASE_TTL", "").strip()
    try:
        return max(60.0, float(raw)) if raw else max(900.0, interval)
    except ValueError:
        return max(900.0, interval)


def prune_scheduled() -> bool:
    return _truthy(os.getenv("EXAMLOPS_AUDIT_PRUNE_SCHEDULED", ""))


def archive_dir() -> Path:
    explicit = os.getenv("EXAMLOPS_AUDIT_ARCHIVE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    root = os.getenv("EXAMLOPS_DATA_DIR", "").strip()
    return Path(root or ".") / "audit-archive"


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


# ── one cycle ────────────────────────────────────────────────────────────────────────────────


def _checkpoint_step(dry_run: bool) -> dict[str, Any]:
    from examlops.audit_worm import checkpoint_and_anchor
    from examlops.data.audit import audit_chain_head, list_audit_checkpoints
    from examlops.supplychain import SigningKeyMissing

    if dry_run:
        head = audit_chain_head()
        last = list_audit_checkpoints(1)
        due = head is not None and not (
            last and last[0]["head_id"] == head["id"] and last[0]["head_hash"] == head["hash"]
        )
        return {"status": "would-checkpoint" if due else "up-to-date", "head": head}
    try:
        res = checkpoint_and_anchor(skip_if_unchanged=True)
    except SigningKeyMissing as exc:
        # Fail closed: no key means no checkpoint - but the schedule keeps running and says so.
        return {"status": "unconfigured", "ok": False, "reason": str(exc)}
    # A WORM write that failed or degraded to the local fallback, or a transparency upload that
    # failed, is a failed step; no anchor configured is not (the signed checkpoint is the job).
    reason = res.get("anchor_error") or res.get("transparency_error")
    out = {**res, "ok": not reason}
    if reason:
        out["reason"] = reason
    return out


def _prune_step(dry_run: bool, actor: str, now: datetime) -> dict[str, Any]:
    from examlops.data.audit_retention import (
        RetentionRefused,
        effective_cutoff,
        execute_prune,
        plan_prune,
        retention_days,
    )

    if not prune_scheduled():
        return {"status": "disabled"}
    try:
        if retention_days() is None:
            return {
                "status": "no-retention-policy",
                "ok": False,
                "reason": "EXAMLOPS_AUDIT_PRUNE_SCHEDULED is on but "
                "EXAMLOPS_AUDIT_RETENTION_DAYS is unset",
            }
        cutoff = effective_cutoff(None, now=now)
        plan = plan_prune(cutoff)
        if dry_run or not plan["eligible"]:
            return {
                "status": "would-prune" if plan["eligible"] else "nothing-to-prune",
                "ok": True,
                **plan,
            }
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        archive = archive_dir() / f"audit-events-upto-{plan['cut_id']}-{stamp}.json"
        res = execute_prune(None, archive_path=str(archive), actor=actor)
    except RetentionRefused as exc:
        return {"status": "refused", "ok": False, "reason": str(exc)}
    return {**res, "ok": True}


def run_cycle(
    *, dry_run: bool = False, holder: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Run one maintenance cycle under the cluster-wide lease. Never raises for a failed step.

    Returns ``{"status": "ok"|"degraded"|"skipped"|"dry-run", "checkpoint": {...},
    "prune": {...}}``. ``degraded`` means a step failed; each step says why.
    """
    from examlops.coordination import get_coordinator

    holder = holder or _holder()
    now = now or datetime.now(UTC)
    actor = os.getenv("EXAMLOPS_ACTOR") or "audit-maintenance"
    coord = get_coordinator()
    ttl = lease_ttl(interval_seconds() or DEFAULT_INTERVAL_S)
    if not dry_run and not coord.try_lock(LEASE_KEY, holder, ttl):
        result: dict[str, Any] = {
            "status": "skipped",
            "reason": "another process holds the maintenance lease",
        }
        _LAST.clear()
        _LAST.update(result)
        return result
    try:
        steps: dict[str, Any] = {}
        for name, fn in (
            ("checkpoint", lambda: _checkpoint_step(dry_run)),
            ("prune", lambda: _prune_step(dry_run, actor, now)),
        ):
            try:
                steps[name] = fn()
            except Exception as exc:  # noqa: BLE001 - one failed step must not stop the next
                logger.exception("audit maintenance step %s failed", name)
                steps[name] = {
                    "status": "error",
                    "ok": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
        failed = [n for n, s in steps.items() if s.get("ok") is False]
        status = "dry-run" if dry_run else ("degraded" if failed else "ok")
        result = {"status": status, "ts": now.isoformat(timespec="seconds"), **steps}
        if failed:
            result["failed_steps"] = failed
            if not dry_run:
                _STEP_FAILURES["count"] += len(failed)
            logger.error(
                "audit maintenance degraded: %s",
                {n: steps[n].get("reason") or steps[n].get("status") for n in failed},
            )
        if not dry_run:
            _record(holder, status, result)
        _LAST.clear()
        _LAST.update(result)
        return result
    finally:
        if not dry_run:
            coord.unlock(LEASE_KEY, holder)


def _record(holder: str, status: str, result: dict[str, Any]) -> None:
    from examlops.data.audit_anchors import record_maintenance_run

    try:
        # Bounded: keep the newest MAX_RUNS_KEPT rows (the schedule's heartbeat, not evidence).
        record_maintenance_run(holder, status, result, keep=MAX_RUNS_KEPT)
    except Exception:  # noqa: BLE001 - the run happened; failing to record it is logged
        logger.exception("could not record the audit maintenance run")


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Recorded cycles, newest first, with the per-step result decoded."""
    from examlops.data.audit_anchors import list_maintenance_runs

    return list_maintenance_runs(max(1, min(int(limit), MAX_RUNS_KEPT)))


def run_forever(stop: threading.Event, *, interval: float | None = None) -> None:
    """Run :func:`run_cycle` every ``interval`` seconds until ``stop`` is set."""
    every = interval if interval is not None else interval_seconds()
    if every <= 0:
        logger.info("audit maintenance schedule disabled (EXAMLOPS_AUDIT_MAINTENANCE_SECONDS=0)")
        return
    logger.info("audit maintenance scheduled every %.0fs", every)
    while not stop.wait(timeout=every):
        try:
            run_cycle()
        except Exception:  # noqa: BLE001 - the loop outlives any one cycle
            _STEP_FAILURES["count"] += 1
            logger.exception("audit maintenance cycle failed")


__all__ = [
    "LEASE_KEY",
    "MAX_RUNS_KEPT",
    "archive_dir",
    "interval_seconds",
    "last_result",
    "list_runs",
    "prune_scheduled",
    "run_cycle",
    "run_forever",
    "step_failures",
]
