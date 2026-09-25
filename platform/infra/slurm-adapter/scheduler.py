"""
Shared scheduler-adapter contract for ExaMLOps.

Defines the common interface that every HPC backend (mock / Slurm / Flux) implements
plus the hardened polling loop they all share. The transport (local subprocess vs SSH)
is an orthogonal concern handled by ``executor.py`` — an adapter is given a
``RemoteExecutor`` and never cares whether commands run locally or over SSH.

Two orthogonal axes:
  * scheduler backend : mock | slurm | flux   (this module + adapter.py / flux_adapter.py)
  * transport         : local | ssh           (executor.py)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

# ── Exceptions ─────────────────────────────────────────────────────────────────


class SchedulerAdapterError(Exception):
    """Base exception for all scheduler adapter failures."""


class JobSubmissionError(SchedulerAdapterError):
    """Failed to submit a job (invalid script, queue full, etc.)."""


class JobNotFoundError(SchedulerAdapterError):
    """Job ID does not exist in the scheduler or mock store."""


class JobTimeoutError(SchedulerAdapterError):
    """A scheduler CLI call or the overall wait exceeded its time budget."""


# ── Shared config ──────────────────────────────────────────────────────────────

# Wall-clock ceiling on any scheduler CLI call so a hung sbatch/squeue/flux can't
# freeze the Prefect worker. Overridable via env.
_CMD_TIMEOUT = int(os.getenv("EXAMLOPS_SLURM_CMD_TIMEOUT", "30"))
# Absolute ceiling on wait_until_complete so a stuck/lost job can't poll forever.
_MAX_WAIT_S = int(os.getenv("EXAMLOPS_SLURM_MAX_WAIT_S", "86400"))  # 24h default
# Consecutive UNKNOWN/errored polls tolerated before declaring the job lost.
_MAX_UNKNOWN_POLLS = int(os.getenv("EXAMLOPS_SLURM_MAX_UNKNOWN_POLLS", "5"))

# Normalized terminal states shared across all backends.
_TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
# Slurm's own spellings of "the job ended badly", which the Slurm adapter reports raw rather than
# as FAILED. Without them a job that lost its node or was preempted — the failures distributed
# training resubmits on (ADR 0032) — was polled as if still running until the 24 h wait ceiling.
_FAILURE_END_STATES = {"NODE_FAIL", "PREEMPTED", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE"}


def normalize_state(state: object) -> str:
    """``"CANCELLED by 1234"`` → ``"CANCELLED"``: sacct appends who cancelled; the rest is noise."""
    text = str(state or "").strip().upper()
    return text.split()[0] if text else "UNKNOWN"


def is_terminal(state: object) -> bool:
    """Whether ``state`` (any backend's spelling) means the job will not run any further."""
    key = normalize_state(state)
    return key in _TERMINAL_STATES or key in _FAILURE_END_STATES


class JobStatus(TypedDict):
    """Normalized status returned by every adapter's ``get_job_status``."""

    state: str
    exit_code: int | None
    start_time: str | None
    end_time: str | None


@runtime_checkable
class SchedulerAdapter(Protocol):
    """The 4-method contract every HPC backend implements."""

    working_dir: Path

    def submit_job(
        self,
        script_path: str | None = ...,
        resources: dict | None = ...,
        training_data: dict | None = ...,
        remote_dir: str | None = ...,
    ) -> str: ...

    def get_job_status(self, job_id: str) -> JobStatus: ...

    def get_job_logs(self, job_id: str) -> str: ...

    def wait_until_complete(
        self, job_id: str, poll_interval: int = ..., max_wait_s: int | None = ...
    ) -> str: ...


class BasePollingAdapter:
    """Mixin providing the hardened ``wait_until_complete`` loop.

    Subclasses implement ``submit_job`` / ``get_job_status`` / ``get_job_logs`` and set
    ``self.working_dir``. The loop is hardened against three failure modes the naive
    ``while True`` had:

      * **No ceiling** → bounded by ``max_wait_s`` (env ``_MAX_WAIT_S``); exceeding it
        raises :class:`JobTimeoutError` instead of hanging forever.
      * **UNKNOWN treated as terminal** → a transient scheduler hiccup (or a job
        momentarily absent) no longer ends the wait early; UNKNOWN/errored polls are
        tolerated up to ``_MAX_UNKNOWN_POLLS`` in a row before the job is declared lost.
      * **CLI hang** → each poll goes through the timeout-bounded executor.
    """

    working_dir: Path

    def get_job_status(self, job_id: str) -> JobStatus:  # pragma: no cover - subclass provides
        raise NotImplementedError

    def wait_until_complete(
        self,
        job_id: str,
        poll_interval: int = 10,
        max_wait_s: int | None = None,
    ) -> str:
        deadline = time.monotonic() + (max_wait_s if max_wait_s is not None else _MAX_WAIT_S)
        unknown_streak = 0

        while True:
            if time.monotonic() > deadline:
                raise JobTimeoutError(
                    f"Job {job_id} did not reach a terminal state within the wait budget"
                )
            try:
                status = self.get_job_status(job_id)
                state = status.get("state", "UNKNOWN")
            except JobNotFoundError:
                state = "UNKNOWN"
            except JobTimeoutError:
                # A single slow scheduler call — treat like an UNKNOWN poll, keep waiting.
                state = "UNKNOWN"

            print(f"[scheduler] job {job_id} → {state}", flush=True)

            if is_terminal(state):
                break
            if state == "UNKNOWN":
                unknown_streak += 1
                if unknown_streak >= _MAX_UNKNOWN_POLLS:
                    raise JobNotFoundError(
                        f"Job {job_id} not observable after {unknown_streak} consecutive polls"
                    )
            else:
                unknown_streak = 0
            time.sleep(poll_interval)

        return str(self.working_dir / f"{job_id}.out")
