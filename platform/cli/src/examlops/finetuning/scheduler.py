"""Run a fine-tune as a job on the phase-23 scheduler (ADR 0044 clause 1, the E6 path).

:func:`scheduler_runner` returns a :data:`~examlops.finetuning.runner.Runner` — the same callable
shape the local subprocess runner has — so :func:`~examlops.finetuning.runner.run_finetune` keeps
one supervision loop (attempts, resume from the last valid checkpoint, FATAL-marker
classification, measured-only registration) whichever executor trained the adapter.

Each attempt:

1. writes a ``run.sh`` through :mod:`examlops.scheduler_jobs` (0700, every value shell-quoted, no
   environment value written into it, kept out of the repository; the job's ``PYTHONPATH`` leads
   with this process's ``examlops``) that ``exec``\\ s the shipped ``train_lora`` script with the
   job interpreter (``EXAMLOPS_HPC_REMOTE_PYTHON`` when set);
2. submits it to mock / Slurm / Flux (``EXAMLOPS_HPC_SCHEDULER``) with the requested resources
   (``gpus``, ``partition``, ``time``, ``account`` …) and records it in ``hpc_jobs`` so
   ``exa hpc jobs`` and the FinOps cost path see it;
3. waits, **bounded** by the run's timeout (the adapters raise instead of hanging), then copies
   the job's log into the attempt log the supervisor parses, and maps the scheduler's terminal
   state to an exit code.

The run directory must be on storage the compute node can write and this host can read (the
shared-filesystem layout the platform deploys on): the job writes its checkpoints, adapter bundle
and FATAL marker there, and the supervisor reads them back.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from examlops.data.audit import audit_best_effort

AUDIT_SOURCE = "exa-finetune"

#: The scheduler resource keys a caller may pass (a subset the phase-23 adapters map to flags).
RESOURCE_KEYS = frozenset(
    {"gpus", "gpus_per_node", "nodes", "cpus_per_task", "mem", "time", "partition", "qos",
     "account", "constraint"}
)  # fmt: skip


class SchedulerUnavailable(RuntimeError):
    """No scheduler adapter could be constructed here."""


def _validated(resources: dict[str, Any] | None) -> dict[str, Any]:
    resources = dict(resources or {})
    unknown = set(resources) - RESOURCE_KEYS
    if unknown:
        raise ValueError(f"unsupported scheduler resource(s): {sorted(unknown)}")
    for key, value in resources.items():
        text = str(value)
        if not text or len(text) > 128 or any(c in text for c in "\n\r\0 "):
            raise ValueError(f"scheduler resource {key}={value!r} is not a single short token")
    return resources


def _exit_code(status: dict[str, Any]) -> int:
    """The script's own exit code when the scheduler reports one, else COMPLETED ⇒ 0, other ⇒ 1."""
    raw = status.get("exit_code")
    if raw is not None:
        try:
            return int(str(raw).split(":", 1)[0])
        except ValueError:
            pass
    return 0 if str(status.get("state") or "").upper() == "COMPLETED" else 1


def scheduler_runner(
    resources: dict[str, Any] | None = None,
    *,
    run_id: str,
    actor: str | None = None,
    adapter: Any = None,
    on_submit: Callable[[str, str], None] | None = None,
) -> Callable[[list[str], dict[str, str], Path, float], int]:
    """A :data:`Runner` that trains through the configured scheduler instead of a local process.

    ``adapter`` is injectable for tests; by default the phase-23 adapter for
    ``EXAMLOPS_HPC_SCHEDULER`` is built once, here, so a misconfigured scheduler fails before
    anything is submitted. ``on_submit(job_id, scheduler)`` is called after each submission.
    """
    from examlops import scheduler_jobs as jobs

    res = _validated(resources)
    if adapter is None:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                adapter = jobs.scheduler_adapter()
        except Exception as exc:  # noqa: BLE001 - no scheduler here is an environment fact
            raise SchedulerUnavailable(f"no scheduler adapter is available: {exc}") from exc
    scheduler = jobs.scheduler_name()

    def run(cmd: list[str], env: dict[str, str], log: Path, timeout: float) -> int:
        # The phase-23 adapters report progress with print(); keep it off stdout, which under
        # `--json` must carry exactly one document.
        with contextlib.redirect_stdout(sys.stderr):
            return _attempt(cmd, log, timeout)

    def _attempt(cmd: list[str], log: Path, timeout: float) -> int:
        argv = [jobs.job_python(), *cmd[1:]]  # the job's interpreter, the same shipped script
        script, job_key = jobs.write_script(
            "finetune", jobs.script_text(argv, title=f"fine-tune {run_id} (ADR 0044)")
        )
        job_resources = {"job_name": f"finetune-{run_id}"[:64], **res}
        try:
            job_id = jobs.submit(adapter, script, job_key, job_resources)
        except Exception as exc:  # noqa: BLE001
            log.write_text(f"[finetune] the {scheduler} scheduler refused the job: {exc}\n")
            audit_best_effort(
                AUDIT_SOURCE,
                actor,
                "finetune_job_refused",
                run_id,
                {"scheduler": scheduler, "error": str(exc)[:300]},
            )
            return 1
        jobs.record_job(job_id, scheduler, f"finetune:{run_id}", job_resources)
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "finetune_job_submitted",
            run_id,
            {"scheduler": scheduler, "hpc_job_id": job_id, "resources": res},
        )
        if on_submit is not None:
            on_submit(job_id, scheduler)
        started = time.monotonic()
        try:
            adapter.wait_until_complete(job_id, max_wait_s=int(max(1, timeout)))
            status = adapter.get_job_status(job_id)
        except Exception as exc:  # noqa: BLE001 - timeout / lost job: this attempt did not finish
            status = {"state": f"UNKNOWN ({type(exc).__name__}: {exc})"[:200], "exit_code": None}
            try:
                adapter.cancel_job(job_id)
            except Exception:  # noqa: BLE001 - best effort; the mock has nothing to cancel
                pass
        jobs.finish_job(job_id, scheduler, status)
        try:
            text = str(adapter.get_job_logs(job_id) or "")
        except Exception as exc:  # noqa: BLE001
            text = f"[finetune] job logs unavailable: {exc}"
        state = str(status.get("state") or "UNKNOWN")
        log.write_text(
            f"{text}\n[finetune] {scheduler} job {job_id} ended {state} "
            f"after {time.monotonic() - started:.1f}s\n"
        )
        return _exit_code(status)

    return run


__all__ = ["RESOURCE_KEYS", "SchedulerUnavailable", "scheduler_runner"]
