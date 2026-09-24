"""Shared admission gate for ``exa`` commands that start real work (ADR 0116 decision 1/3).

The seam's ``decide()`` and its two-phase reservation existed with no caller: admission was
something you could *simulate*. This is the CLI half of the first real dispatcher — one context
manager a command wraps around the work it is about to start.

Contract, deliberately the same shape as :mod:`examlops.cli._policy_gate`:

* ``EXAMLOPS_ADMISSION_DISPATCH_ENABLED`` unset or falsy (**the default**) → nothing happens. No
  datastore is opened, no decision is taken, no reservation is made, no audit row is written and
  nothing is printed, so the command is byte-identical to what it was before the gate existed.
* Enabled and admitted → the reservation is held for the duration of the block and released when
  the block ends, however it ends (success, exception, Ctrl-C).
* Enabled and refused → the reason the policy (or the quota) gave, exit code 1 (``_output.error``
  exits), and an ``admission_refused`` audit row written by the seam.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator

from . import _output


@contextlib.contextmanager
def pipeline_run_gate(
    *, project: str | None = None, model: str | None = None, gpus: int = 0
) -> Iterator[None]:
    """Admit a training run before it starts; hold its quota until it finishes.

    Imported lazily by its caller and short-circuiting on the kill-switch before it imports the
    seam, so the default path costs one environment read.
    """
    from examlops.admission_seam import dispatch

    if not dispatch.is_enabled():
        yield
        return

    request = dispatch.request_for_pipeline_run(project=project, gpus=gpus)
    # One holder per run, not per model: two concurrent runs of the same model must not share a
    # reservation, or the first to finish would release the second's quota.
    holder = dispatch.completion.holder_for_run(f"pipeline-{model or 'all'}-{uuid.uuid4().hex[:8]}")
    try:
        with dispatch.admitted(request, holder=holder) as admission:
            _output.detail(
                f"  admission: {admission.decision.get('verdict')} "
                f"({admission.decision.get('reason')}) — reservation {admission.reservation}"
            )
            yield
    except dispatch.AdmissionRefused as exc:
        _output.error(
            f"Admission refused this run ({exc.verdict}): {exc.reason}",
            hint="Inspect with: exa admission reservations — or unset "
            "EXAMLOPS_ADMISSION_DISPATCH_ENABLED to run without the gate",
        )
