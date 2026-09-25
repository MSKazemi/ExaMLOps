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
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from examlops.data import quota_reservations as store
from examlops.data.audit import audit_best_effort

from . import completion, reservations
from .policy import Admit
from .request import JobRequest, Resources
from .service import decide

ENABLED_ENV = "EXAMLOPS_ADMISSION_DISPATCH_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}
_SOURCE = "admission-seam"
log = logging.getLogger(__name__)


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


def _whole(key: str, raw: Any, *, upper: bool = False) -> int | None:
    """A whole count from an adapter resource value, or ``None`` when absent.

    ``upper`` reads a Slurm range (``--nodes=2-4``) as its upper bound: admission holds quota for
    the most the scheduler may allocate, never the least.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    if upper and "-" in text:
        text = text.rsplit("-", 1)[-1].strip()
    if text.isdigit():
        return int(text)
    raise ValueError(f"cannot read a whole count from {key}={raw!r}")


def _gpu_total(key: str, raw: Any) -> int | None:
    """GPUs named by a Slurm/Flux GPU value: ``4``, ``a100:4`` or ``a100:2,v100:1``.

    Anything else **raises**. Reading an unparseable GPU value as "no GPUs" would admit a GPU job
    against a zero-GPU reservation — quota enforcement failing open on a spelling.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    total = 0
    for part in text.split(","):
        count = part.strip().rsplit(":", 1)[-1].strip()
        if not count.isdigit():
            raise ValueError(
                f"cannot read a GPU count from {key}={raw!r}; refusing to admit it as 0 GPUs"
            )
        total += int(count)
    return total


def request_for_hpc_job(
    resources: dict[str, Any] | None,
    *,
    project: str | None = None,
    tenant: str | None = None,
    workload_class: str = "hpc-job",
    scheduler: str | None = None,
) -> JobRequest:
    """The ``JobRequest`` a scheduler submission implies, read from its adapter ``resources`` dict.

    GPUs are counted the way the **backend** will allocate them, because this is what the quota is
    held against:

    * ``gpus`` — Slurm (and the mock) read ``--gpus`` as the job total; Flux reads ``-g`` as GPUs
      *per slot*, and a ``flux batch`` with no ``ntasks`` has one slot per node, so the total is
      ``gpus x (ntasks or nodes)``;
    * ``gpus_per_node`` — ``x nodes`` on every backend.

    The larger reading wins, a node range counts its upper bound, and a GPU value that is not a
    count raises ``ValueError`` (the caller then submits nothing) instead of being admitted as zero.
    Attribution follows :func:`request_for_pipeline_run`: the active project, else ``default``.
    """
    res = resources or {}
    nodes = _whole("nodes", res.get("nodes"), upper=True) or 1
    per_slot = (scheduler or "").strip().lower() == "flux"
    slots = (_whole("ntasks", res.get("ntasks")) or nodes) if per_slot else 1
    candidates = [0]
    gpus = _gpu_total("gpus", res.get("gpus"))
    if gpus is not None:
        candidates.append(gpus * slots)
    per_node = _gpu_total("gpus_per_node", res.get("gpus_per_node"))
    if per_node is not None:
        candidates.append(per_node * nodes)
    try:
        cpus = _whole("cpus_per_task", res.get("cpus_per_task")) or 0
    except ValueError:
        cpus = 0  # CPUs are recorded, not quota'd: an odd spelling is not a reason to refuse

    return JobRequest(
        project=project or os.getenv("EXAMLOPS_PROJECT") or "default",
        resources=Resources(gpus=max(candidates), cpus=cpus, nodes=nodes),
        tenant=tenant or os.getenv("EXAMLOPS_TENANT") or "default",
        workload_class=workload_class,
    )


def submit_admitted(
    request: JobRequest,
    *,
    scheduler: str,
    submit: Callable[[], str],
    actor: str | None = None,
    ttl_s: float | None = None,
) -> str:
    """Admit a scheduler submission, run ``submit()``, and hand the quota to the job it created.

    The asynchronous counterpart of :func:`admitted`: a submitted job outlives this call, so the
    reservation cannot be released in a ``finally``. Instead, once ``submit()`` returns a job id the
    row is rebound to :func:`completion.holder_for_job` *and* committed in one statement
    (:func:`examlops.data.quota_reservations.bind_and_commit`). From then on it is released at the
    one chokepoint every terminal job state passes through — ``examlops.data.hpc.update_hpc_job`` —
    exactly as ADR 0116 decision 3 describes. The rebind happens before the caller records the job
    in ``hpc_jobs``, so no poller can see a terminal state for a job whose quota is not yet bound.

    Refused -> :class:`AdmissionRefused` and nothing is submitted. ``submit()`` raising -> the
    reservation is released (``failed``) and the error propagates. Disabled -> ``submit()`` only.
    """
    if not is_enabled():
        return submit()

    request.validate()
    actor = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER")
    pending = completion.holder_for_run(f"submit-{uuid.uuid4().hex[:12]}")
    decision, meta = decide(request, actor=actor)
    if not isinstance(decision, Admit):
        _audit(
            "admission_refused",
            request,
            {"holder": pending, "policy": meta["policy"], "scheduler": scheduler}
            | decision.to_dict(),
            actor,
        )
        raise AdmissionRefused(decision.reason, verdict=decision.verdict, request=request)

    out = reservations.reserve(request, ttl_s=ttl_s, holder=pending, actor=actor)
    if not out["ok"]:
        _audit(
            "admission_refused",
            request,
            {
                "holder": pending,
                "policy": meta["policy"],
                "scheduler": scheduler,
                "verdict": "reject",
                "reason": out["reason"],
            },
            actor,
        )
        raise AdmissionRefused(out["reason"], verdict="reject", request=request)

    rid = out["reservation"]["id"]
    try:
        job_id = str(submit())
    except BaseException:
        completion.release_on_completion(pending, outcome="failed", actor=actor)
        raise
    holder = completion.holder_for_job(scheduler, job_id)
    try:
        bound = store.bind_and_commit(rid, holder)
    except Exception as exc:  # noqa: BLE001 - the job exists; raising would orphan it
        # The scheduler already accepted the job. Raising here would tell the caller the
        # submission failed while a real job runs untracked (and, for a server, unstoppable by
        # name). Report the bookkeeping failure instead; the row lapses at its TTL.
        log.warning(
            "admission: job %s:%s submitted but its quota was not bound: %s", scheduler, job_id, exc
        )
        _audit(
            "quota_bind_failed",
            request,
            {
                "id": rid,
                "holder": holder,
                "scheduler": scheduler,
                "job_id": job_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            actor,
        )
        return job_id
    if bound:
        _audit(
            "quota_committed",
            request,
            {"id": rid, "holder": holder, "scheduler": scheduler, "job_id": job_id},
            actor,
        )
    else:
        # The TTL lapsed between reserve and submit (a very slow scheduler). The job is already
        # queued, so it is not cancelled; the lapse is recorded so the unheld job is visible.
        _audit(
            "quota_bind_lapsed",
            request,
            {"id": rid, "holder": holder, "scheduler": scheduler, "job_id": job_id},
            actor,
        )
    return job_id
