"""
Dual-mode scheduler adapter factory for ExaMLOps.

Scheduler backends (which queue runs the job):
- mock  : local fake scheduler — runs real sklearn training locally (no HPC needed)
- slurm : real Slurm integration (sbatch/squeue/sacct)
- flux  : real Flux integration (flux batch/jobs) — see flux_adapter.py

Transport (how commands reach the cluster) is orthogonal and lives in executor.py
(local subprocess vs SSH). See scheduler.py for the shared contract + wait loop.

Selection via env (back-compatible):
  EXAMLOPS_HPC_SCHEDULER=mock|slurm|flux   (preferred)
  EXAMLOPS_SLURM_MODE=mock|slurm           (legacy; used when the above is unset)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from executor import LocalExecutor, RemoteExecutor, get_executor
from scheduler import (
    _CMD_TIMEOUT,
    BasePollingAdapter,
    JobNotFoundError,
    JobStatus,
    JobSubmissionError,
    JobTimeoutError,
    SchedulerAdapterError,
)

# ── Back-compat exception aliases ───────────────────────────────────────────────
# Older call sites do ``from adapter import SlurmAdapterError`` etc.
SlurmAdapterError = SchedulerAdapterError

__all__ = [
    "SlurmAdapterError",
    "SchedulerAdapterError",
    "JobSubmissionError",
    "JobNotFoundError",
    "JobTimeoutError",
    "RealSlurmAdapter",
    "get_scheduler_adapter",
    "get_slurm_adapter",
]

# Map resource-dict keys → sbatch flag names
_RESOURCE_FLAGS: dict[str, str] = {
    "partition": "--partition",
    "qos": "--qos",
    "time": "--time",
    "nodes": "--nodes",
    "ntasks": "--ntasks",
    "cpus_per_task": "--cpus-per-task",
    "mem": "--mem",
    "gpus": "--gpus",
    "constraint": "--constraint",
    "output": "--output",
    "error": "--error",
    "job_name": "--job-name",
    "account": "--account",
}


class RealSlurmAdapter(BasePollingAdapter):
    """Real Slurm adapter — runs sbatch/squeue/sacct through a RemoteExecutor."""

    def __init__(
        self,
        executor: RemoteExecutor | None = None,
        working_dir: str = "slurm_jobs",
    ):
        self.executor: RemoteExecutor = executor or LocalExecutor()
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self._jobdir_hint: str | None = None

    def submit_job(
        self,
        script_path: str | None = None,
        resources: dict | None = None,
        training_data: dict | None = None,  # unused in real mode
        remote_dir: str | None = None,
    ) -> str:
        """Submit a job script via sbatch. Returns the Slurm job ID string.

        When ``remote_dir`` is given the script is staged there through the executor
        (SSH-friendly) and job stdout/stderr default under it; the caller fetches
        ``<remote_dir>/model.pkl`` afterward. Without it, behavior matches the historical
        shared-filesystem path.
        """
        if not script_path:
            raise JobSubmissionError("script_path is required for real Slurm mode")

        script = Path(script_path)
        if not script.exists():
            raise JobSubmissionError(f"Job script not found: {script}")

        submit_target = str(script)
        run_cwd: str | None = str(script.parent)
        if remote_dir:
            self.executor.run(["mkdir", "-p", remote_dir])
            submit_target = f"{remote_dir}/run.sh"
            self.executor.put(str(script), submit_target)
            self._jobdir_hint = remote_dir
            run_cwd = remote_dir
            resources = dict(resources or {})
            resources.setdefault("output", f"{remote_dir}/%j.out")
            resources.setdefault("error", f"{remote_dir}/%j.err")

        cmd = ["sbatch"]
        for key, flag in _RESOURCE_FLAGS.items():
            if resources and key in resources:
                cmd.append(f"{flag}={resources[key]}")

        # Default log paths next to this adapter's working dir so logs are easy to find
        if not resources or "output" not in resources:
            cmd.append(f"--output={self.working_dir / '%j.out'}")
        if not resources or "error" not in resources:
            cmd.append(f"--error={self.working_dir / '%j.err'}")

        cmd.append(submit_target)

        result = self.executor.run(cmd, timeout=_CMD_TIMEOUT, cwd=run_cwd)
        if result.returncode != 0:
            raise JobSubmissionError(f"sbatch failed: {result.stderr.strip()}")

        tokens = result.stdout.strip().split()
        if not tokens:
            raise JobSubmissionError("sbatch returned no output")
        return tokens[-1]  # "Submitted batch job 12345678" → "12345678"

    def get_job_status(self, job_id: str) -> JobStatus:
        """Return status dict. Tries squeue (active jobs) then sacct (history)."""
        sq = self.executor.run(
            ["squeue", "-j", job_id, "-h", "-o", "%T|%S|%e"], timeout=_CMD_TIMEOUT
        )
        line = sq.stdout.strip()
        if line:
            parts = line.split("|")
            return {
                "state": parts[0] if parts else "UNKNOWN",
                "exit_code": None,
                "start_time": parts[1] if len(parts) > 1 and parts[1] != "N/A" else None,
                "end_time": parts[2] if len(parts) > 2 and parts[2] != "N/A" else None,
            }

        sa = self.executor.run(
            ["sacct", "-j", job_id, "--format=State,ExitCode,Start,End", "--noheader", "-P"],
            timeout=_CMD_TIMEOUT,
        )
        for raw in sa.stdout.strip().splitlines():
            parts = raw.split("|")
            if len(parts) < 4:
                continue
            state, exit_raw, start, end = parts[:4]
            code_str = exit_raw.split(":")[0] if exit_raw else None
            exit_code = int(code_str) if code_str and code_str.isdigit() else None
            return {
                "state": state.strip(),
                "exit_code": exit_code,
                "start_time": start.strip() or None,
                "end_time": end.strip() or None,
            }

        raise JobNotFoundError(f"Job {job_id} not found in squeue or sacct")

    def get_job_logs(self, job_id: str) -> str:
        """Return stdout log content for the job."""
        default_log = self.working_dir / f"{job_id}.out"
        if default_log.exists():
            return default_log.read_text()

        # Ask sacct where Slurm wrote the log file
        sa = self.executor.run(
            ["sacct", "-j", job_id, "--format=StdOut", "--noheader", "-P"], timeout=_CMD_TIMEOUT
        )
        for line in sa.stdout.strip().splitlines():
            path = line.strip()
            if not path:
                continue
            local = self.working_dir / f"{job_id}.out"
            try:
                self.executor.get(path, str(local))
                return local.read_text()
            except (FileNotFoundError, OSError):
                continue

        return f"[SlurmAdapter] No log found for job {job_id}"

    def remote_jobdir(self, job_id: str) -> str:
        """Per-job dir where the training script wrote ``model.pkl``.

        Uses the staged ``remote_dir`` when submission staged over the executor;
        otherwise the shared-FS convention ``working_dir/<job_id>``.
        """
        return self._jobdir_hint or str(self.working_dir / job_id)


# ── Factory ────────────────────────────────────────────────────────────────────


def _ensure_on_path() -> None:
    here = Path(__file__).parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


def _resolve_scheduler() -> str:
    """Resolve the scheduler backend, honoring the legacy EXAMLOPS_SLURM_MODE."""
    sched = os.getenv("EXAMLOPS_HPC_SCHEDULER", "").lower().strip()
    if sched:
        return sched
    legacy = os.getenv("EXAMLOPS_SLURM_MODE", "mock").lower().strip()
    return "slurm" if legacy == "slurm" else "mock"


def get_scheduler_adapter(executor: RemoteExecutor | None = None):
    """Return the adapter for EXAMLOPS_HPC_SCHEDULER (mock|slurm|flux)."""
    _ensure_on_path()
    sched = _resolve_scheduler()

    if sched == "mock":
        from mock_slurm_adapter import MockSlurmAdapter  # noqa: PLC0415

        print("[scheduler] backend=mock (local)", flush=True)
        return MockSlurmAdapter()

    executor = executor or get_executor()
    if sched == "flux":
        from flux_adapter import FluxAdapter  # noqa: PLC0415

        print("[scheduler] backend=flux (real HPC)", flush=True)
        return FluxAdapter(executor=executor)
    if sched == "slurm":
        print("[scheduler] backend=slurm (real HPC)", flush=True)
        return RealSlurmAdapter(executor=executor)

    raise SchedulerAdapterError(f"unknown EXAMLOPS_HPC_SCHEDULER={sched!r}")


def get_slurm_adapter():
    """Back-compat alias — selects the adapter per env (see get_scheduler_adapter)."""
    return get_scheduler_adapter()
