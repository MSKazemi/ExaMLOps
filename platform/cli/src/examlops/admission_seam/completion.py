"""Release a reservation when its holder reaches a terminal state (ADR 0116 decision 3, part 2).

Decision 3 is *"quota is reserved at admission **and released on completion**"*. The reserve half
shipped with the seam; this is the release half, and the ADR's verification item 3 says it plainly:
released **including on failure**. A reservation that outlives its job is a leak that silently
shrinks the project's headroom until someone reads the table.

**One release function, one chokepoint.** Everything here funnels into
:func:`release_on_completion`, and the platform calls it from exactly one place in the job
lifecycle: :func:`examlops.data.hpc.update_hpc_job`, the single function through which *every*
``hpc_jobs`` state write passes — both callers (``examlops.scheduler_jobs.finish_job`` and
``pipelines.pipeline_generator._update_hpc_job_safe``) reach it, and it sits below the
mock/slurm/flux branch, so all three schedulers converge on it. The alternative — calling release
next to each adapter's wait loop — would be three sites that must each be remembered, and the mock
path does not have one at all.

The second caller is not a second chokepoint but a *scope guard*: a reservation whose holder is the
dispatching call itself (:mod:`examlops.admission_seam.dispatch`) is released in that call's
``finally``, which is the same function with the same holder vocabulary.

**The TTL sweep stays the backstop, not the mechanism.** A holder that is killed between reserving
and completing never reaches either site; its row lapses at ``expires_at`` (holding nothing from
that instant) and ``exa admission reconcile`` marks it ``expired``. A *committed* job
reservation is never TTL-swept (a running job outlives any TTL); ``exa admission reconcile``
also asks the scheduler about each one and releases those whose job has ended
(:func:`reconcile_job_reservations`). Completion and expiry can race — they resolve the same row under the same scoped write lock, so exactly one of them wins and the
other reports that it released nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from examlops.data import quota_reservations as store
from examlops.data.audit import audit_best_effort

log = logging.getLogger(__name__)

_SOURCE = "admission-seam"

#: The three terminal outcomes. Identical to :data:`examlops.operations.TERMINAL`, deliberately:
#: an operation and a job are the same lifecycle seen from two heights.
OUTCOMES = ("completed", "failed", "cancelled")

#: Scheduler job states → outcome. The phase-23 adapters normalise to ``COMPLETED``/``FAILED``/
#: ``CANCELLED``/``TIMEOUT`` (``scheduler._TERMINAL_STATES``); the raw Slurm spellings are mapped
#: too, so a state that reaches the table unnormalised still releases rather than leaking.
_JOB_STATES: dict[str, str] = {
    "COMPLETED": "completed",
    "COMPLETING": "",  # still running — listed so it is not silently read as terminal
    "FAILED": "failed",
    "TIMEOUT": "failed",
    "NODE_FAIL": "failed",
    "OUT_OF_MEMORY": "failed",
    "BOOT_FAIL": "failed",
    "DEADLINE": "failed",
    "SPECIAL_EXIT": "failed",
    "CANCELLED": "cancelled",
    "REVOKED": "cancelled",
    "PREEMPTED": "cancelled",
}


def normalize_outcome(state: str | None) -> str | None:
    """``state`` as one of :data:`OUTCOMES`, or ``None`` when it is not terminal.

    ``None`` is returned for anything unrecognised as well as for a running state: guessing a
    strange state into ``failed`` would release a reservation out from under a job that is still
    holding the GPUs. A leak is visible in the table and swept by TTL; an early release is not
    visible at all.
    """
    if not state:
        return None
    # sacct writes "CANCELLED by 1234" and truncates to "CANCELLED+"; the first word is the state.
    words = str(state).strip().upper().split()
    if not words:
        return None
    key = words[0].rstrip("+")
    if key.lower() in OUTCOMES:  # the operation vocabulary, and COMPLETED/FAILED/CANCELLED
        return key.lower()
    return _JOB_STATES.get(key) or None


def holder_for_job(scheduler: str, job_id: str) -> str:
    """The holder string for a scheduler job. One vocabulary, written once."""
    return f"hpc:{scheduler}:{job_id}"


def holder_for_run(run_id: str) -> str:
    """The holder string for a dispatching call that holds a reservation for its own duration."""
    return f"run:{run_id}"


def release_on_completion(
    holder: str,
    *,
    outcome: str,
    actor: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Release everything ``holder`` holds because it reached ``outcome``.

    Idempotent: a second call (or a call for a holder that never reserved) releases nothing and
    says so. Every genuinely-released reservation is audited as ``quota_released`` through
    ``audit_best_effort``, so a lost record is counted rather than passed over.

    Returns ``{"holder", "outcome", "released": n, "reservations": [ids], "gpus", "gpu_hours"}``.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {list(OUTCOMES)}, got {outcome!r}")
    why = reason or f"holder {outcome}"
    rows = store.release_by_holder(holder, reason=why)
    for row in rows:
        audit_best_effort(
            _SOURCE,
            actor,
            "quota_released",
            row["project"],
            {
                "id": row["id"],
                "holder": holder,
                "outcome": outcome,
                "reason": why,
                "gpus": row["gpus"],
                "gpu_hours": row["gpu_hours"],
            },
            tenant=row["tenant"],
        )
    return {
        "holder": holder,
        "outcome": outcome,
        "released": len(rows),
        "reservations": [r["id"] for r in rows],
        "gpus": sum(int(r["gpus"] or 0) for r in rows),
        "gpu_hours": sum(float(r["gpu_hours"] or 0.0) for r in rows),
    }


def reconcile_job_reservations(
    status_of: Callable[[str, str], str | None],
    *,
    actor: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Release committed job reservations whose job the scheduler says has ended.

    The terminal-state chokepoint (:func:`on_job_terminal_state`) only runs when someone records a
    terminal state, and several submissions never do: a serving allocation that hits its wall time
    or crashes (only ``stop()`` releases it), a Slurm/Flux reindex that succeeds (only failures
    are reconciled there), an asset build whose wait timed out. A ``committed`` row is exempt from
    the TTL sweep by design — a running job outlives any admission TTL — so without this those
    rows would hold their GPUs for ever.

    ``status_of(scheduler, job_id)`` returns the scheduler's state for the job. A state that is not
    recognisably terminal (including ``UNKNOWN``), or a lookup that raises, keeps the reservation:
    releasing quota under a job that may still hold the GPUs is the invisible failure; a leak is
    visible here and in ``exa admission reservations``.
    """
    released: list[dict[str, Any]] = []
    still_running: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for holder in store.committed_job_holders():
        _, _, rest = holder.partition(":")
        scheduler, _, job_id = rest.partition(":")
        if not scheduler or not job_id:
            unverified.append({"holder": holder, "error": "malformed job holder"})
            continue
        try:
            state = status_of(scheduler, job_id)
        except Exception as exc:  # noqa: BLE001 - one unreachable scheduler must not stop the rest
            unverified.append({"holder": holder, "error": f"{type(exc).__name__}: {exc}"})
            continue
        outcome = normalize_outcome(state)
        entry: dict[str, Any] = {"holder": holder, "state": state}
        if outcome is None:
            still_running.append(entry)
            continue
        if dry_run:
            released.append(entry | {"outcome": outcome, "dry_run": True})
            continue
        out = release_on_completion(
            holder, outcome=outcome, actor=actor, reason=f"reconciled: job {state}"
        )
        if out["released"]:
            released.append(entry | {"outcome": outcome, "gpus": out["gpus"]})
    return {"released": released, "still_running": still_running, "unverified": unverified}


def on_job_terminal_state(job_id: str, scheduler: str, state: str | None) -> dict[str, Any] | None:
    """The chokepoint hook. ``None`` when ``state`` is not terminal or nothing was held.

    Called from :func:`examlops.data.hpc.update_hpc_job`. Bookkeeping must never fail a job, so a
    datastore failure here is logged at WARNING (never swallowed silently) and the job's own state
    write stands — the TTL sweep then reclaims what this call could not.
    """
    outcome = normalize_outcome(state)
    if outcome is None:
        return None
    holder = holder_for_job(scheduler, job_id)
    try:
        out = release_on_completion(holder, outcome=outcome, reason=f"job {state}")
    except Exception as exc:  # noqa: BLE001 - a job's state must be recorded regardless
        log.warning(
            "admission: could not release reservations held by %s (%s); "
            "they now depend on the TTL sweep: %s",
            holder,
            state,
            exc,
        )
        return None
    return out if out["released"] else None
