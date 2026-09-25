"""ADR 0030 decision 5 — fractional GPU-hours flow into FinOps, carbon and ``exa models cost``.

A scheduler reports GPU-hours per *device*: Slurm's ``sacct`` counts a MIG slice as one
``gres/gpu``, and a time-sliced device is a whole device to its accounting. Billing those hours
unscaled charges a job that held a 1g.5gb slice for a whole A100. This module scales a job's
device-hours by the fraction it was **allocated** — read from the ``gpu_allocations`` row the
pipeline linked to that job at submit time — and nothing else:

* no allocation linked to the job → the device-hours are returned unchanged and the fraction is
  ``None`` (never a guessed fraction from some other allocation of the same model);
* ``whole`` (including the honest fallback) → fraction 1.0, unchanged, the waste already paid;
* ``mig`` / ``timeslice`` → device-hours × allocated fraction.

``model_costs.gpu_hours`` is what ``exa finops`` budgets, carbon (``exa finops carbon``) and the
project cost roll-ups read, so scaling it at record time is what carries the fraction to all of
them; ``model_costs.gpu_fraction``/``gpu_mechanism`` keep the provenance of the scaling.
"""

from __future__ import annotations

from dataclasses import dataclass

from examlops.gpu_sharing import allocation_for_job


@dataclass(frozen=True)
class FractionalBill:
    gpu_hours: float | None
    device_gpu_hours: float | None
    fraction: float | None
    mechanism: str | None

    @property
    def scaled(self) -> bool:
        return self.fraction is not None and self.fraction < 1.0


def bill_job(
    job_id: str | None, device_gpu_hours: float | None, *, scheduler: str | None = None
) -> FractionalBill:
    """Scale ``device_gpu_hours`` by the GPU fraction ``job_id`` was allocated (see module doc).

    ``scheduler`` scopes the lookup: a job id is only unique within its scheduler.
    """
    alloc = allocation_for_job(job_id, scheduler=scheduler) if job_id else None
    if alloc is None:
        return FractionalBill(device_gpu_hours, device_gpu_hours, None, None)
    fraction = float(alloc["fraction"])
    if not (0.0 < fraction <= 1.0):
        # A corrupt row must not zero or inflate a bill; bill the device-hours and say so.
        return FractionalBill(device_gpu_hours, device_gpu_hours, None, alloc.get("mechanism"))
    billed = None if device_gpu_hours is None else device_gpu_hours * fraction
    return FractionalBill(billed, device_gpu_hours, fraction, str(alloc["mechanism"]))


__all__ = ["FractionalBill", "bill_job"]
