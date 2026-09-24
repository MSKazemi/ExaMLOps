"""Dispatch *through* the seam: ask before running, hold a reservation, release on completion.

ADR 0116 built ``decide()`` and the two-phase reservation and then left them unused — nothing in
the platform consulted the seam before starting work, so the decision was a thing you could
simulate (``exa admission simulate``) and not a thing that governed anything. This module is the
smallest real dispatcher, and :func:`examlops.cli.commands.pipeline.run` is its first caller.

**Off by default, and byte-identical when off.** ``EXAMLOPS_ADMISSION_DISPATCH_ENABLED`` follows
the house kill-switch pattern (``EXAMLOPS_AUTOPILOT_ENABLED``, ``EXAMLOPS_AUTOSCALE_ENABLED``):
only an explicit truthy value arms it. Disabled, :func:`admitted` opens no datastore, builds no
request, takes no lock and writes no audit row — it yields a context saying ``enabled: False`` and
the caller runs exactly as it did before. That is why the gate can be added to a working command
without a behaviour change to prove.

**Enabled, a refusal is a refusal.** A ``Queue`` or ``Reject`` verdict, or a reservation the
project's quota cannot fit, raises :class:`AdmissionRefused` with the policy's own reason text and
an ``admission_refused`` audit row. It is not downgraded to a warning: the point of admitting above
execution is that the work does not start.

**The reservation's lifetime is the call's.** :func:`admitted` is a context manager; its ``finally``
releases through :func:`examlops.admission_seam.completion.release_on_completion`, so a failure or
a ``KeyboardInterrupt`` returns the quota just as a success does. A process killed outright reaches
no ``finally`` at all — that is what the TTL backstop is for, and it is the only path that needs it.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from typing import Any

from examlops.data.audit import audit_best_effort

from . import completion, reservations
from .policy import Admit
from .request import JobRequest, Resources
from .service import decide

ENABLED_ENV = "EXAMLOPS_ADMISSION_DISPATCH_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}
_SOURCE = "admission-seam"


class AdmissionRefused(RuntimeError):
    """The seam declined to admit the work; nothing was started and nothing is held."""

    def __init__(self, reason: str, *, verdict: str, request: JobRequest):
        self.reason = reason
        self.verdict = verdict
        self.request = request
        super().__init__(reason)


def is_enabled() -> bool:
    """The kill-switch. Default OFF: only an explicit truthy value routes work through the seam."""
    return os.getenv(ENABLED_ENV, "").strip().lower() in _TRUTHY


class Admission:
    """What :func:`admitted` yields: whether the seam ran, and what it decided."""

    def __init__(
        self,
        *,
        enabled: bool,
        holder: str | None = None,
        reservation: str | None = None,
        decision: dict[str, Any] | None = None,
    ):
        self.enabled = enabled
        self.holder = holder
        self.reservation = reservation
        self.decision = decision or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "holder": self.holder,
            "reservation": self.reservation,
            "decision": self.decision,
        }


def _audit(action: str, request: JobRequest, details: dict[str, Any], actor: str | None) -> None:
    audit_best_effort(_SOURCE, actor, action, request.project, details, tenant=request.tenant)


@contextlib.contextmanager
def admitted(
    request: JobRequest,
    *,
    actor: str | None = None,
    holder: str | None = None,
    ttl_s: float | None = None,
) -> Iterator[Admission]:
    """Admit ``request``, hold its quota for the block, release it however the block ends.

    Disabled (the default) this is a no-op context: nothing is decided, reserved or audited.
    Enabled it raises :class:`AdmissionRefused` *before* yielding when the policy queues/rejects
    the request or the reservation does not fit.
    """
    if not is_enabled():
        yield Admission(enabled=False)
        return

    request.validate()
    actor = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER")
    key = holder or completion.holder_for_run(uuid.uuid4().hex[:12])

    decision, meta = decide(request, actor=actor)
    if not isinstance(decision, Admit):
        _audit(
            "admission_refused",
            request,
            {"holder": key, "policy": meta["policy"], **decision.to_dict()},
            actor,
        )
        raise AdmissionRefused(decision.reason, verdict=decision.verdict, request=request)

    out = reservations.reserve(request, ttl_s=ttl_s, holder=key, actor=actor)
    if not out["ok"]:
        _audit(
            "admission_refused",
            request,
            {
                "holder": key,
                "policy": meta["policy"],
                "verdict": "reject",
                "reason": out["reason"],
            },
            actor,
        )
        raise AdmissionRefused(out["reason"], verdict="reject", request=request)

    rid = out["reservation"]["id"]
    state = Admission(
        enabled=True,
        holder=key,
        reservation=rid,
        decision={**decision.to_dict(), "policy": meta["policy"]},
    )
    outcome = "failed"
    try:
        yield state
        outcome = "completed"
    except (KeyboardInterrupt, SystemExit):
        outcome = "cancelled"
        raise
    finally:
        completion.release_on_completion(key, outcome=outcome, actor=actor)


def request_for_pipeline_run(
    *,
    project: str | None = None,
    gpus: int = 0,
    tenant: str | None = None,
) -> JobRequest:
    """The ``JobRequest`` a training run submits. ``project`` falls back to the active project.

    ``project`` is required by the vocabulary and is what quota is held against; with none set
    anywhere the run is attributed to ``default``, the same name the queue already uses for an
    unscoped tenant. Nothing else is inferred: the model is not a field of the vocabulary and is
    carried in the holder string, not smuggled into ``queue`` (which names an admission queue).
    """
    name = project or os.getenv("EXAMLOPS_PROJECT") or "default"
    return JobRequest(
        project=name,
        resources=Resources(gpus=max(0, int(gpus or 0))),
        tenant=tenant or os.getenv("EXAMLOPS_TENANT") or "default",
        workload_class="training",
    )
