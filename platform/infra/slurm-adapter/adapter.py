"""
Dual-mode Slurm adapter factory for ExaMLOps.

Modes:
- mock  : local fake Slurm — runs real sklearn training locally (no HPC needed)
- slurm : real Slurm integration for HPC environments

Mode is selected via:
  EXAMLOPS_SLURM_MODE=mock|slurm  (default: mock)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# ── Exceptions ─────────────────────────────────────────────────────────────────


class SlurmAdapterError(Exception):
    """Base exception for all Slurm adapter failures."""


class JobSubmissionError(SlurmAdapterError):
    """Failed to submit a job (invalid script, queue full, etc.)."""


class JobNotFoundError(SlurmAdapterError):
    """Job ID does not exist in Slurm or mock store."""


# Map resource-dict keys → sbatch flag names
_RESOURCE_FLAGS: Dict[str, str] = {
    "partition":     "--partition",
    "time":          "--time",
    "nodes":         "--nodes",
    "ntasks":        "--ntasks",
    "cpus_per_task": "--cpus-per-task",
    "mem":           "--mem",
    "gpus":          "--gpus",
    "output":        "--output",
    "error":         "--error",
    "job_name":      "--job-name",
    "account":       "--account",
}

_TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}


class RealSlurmAdapter:
    """Real Slurm adapter — calls sbatch/squeue/sacct on an HPC cluster."""

    def __init__(self, working_dir: str = "slurm_jobs"):
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)

    def submit_job(
        self,
        script_path: Optional[str] = None,
        resources: Optional[Dict] = None,
        training_data: Optional[Dict] = None,  # unused in real mode
    ) -> str:
        """Submit a job script via sbatch. Returns the Slurm job ID string."""
        if not script_path:
            raise JobSubmissionError("script_path is required for real Slurm mode")

        script = Path(script_path)
        if not script.exists():
            raise JobSubmissionError(f"Job script not found: {script}")

        cmd = ["sbatch"]
        for key, flag in _RESOURCE_FLAGS.items():
            if resources and key in resources:
                cmd.append(f"{flag}={resources[key]}")

        # Default log paths next to this adapter's working dir so logs are easy to find
        if not resources or "output" not in resources:
            cmd.append(f"--output={self.working_dir / '%j.out'}")
        if not resources or "error" not in resources:
            cmd.append(f"--error={self.working_dir / '%j.err'}")

        cmd.append(str(script))

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(script.parent))
        if result.returncode != 0:
            raise JobSubmissionError(f"sbatch failed: {result.stderr.strip()}")

        tokens = result.stdout.strip().split()
        if not tokens:
            raise JobSubmissionError("sbatch returned no output")
        return tokens[-1]  # "Submitted batch job 12345678" → "12345678"

    def get_job_status(self, job_id: str) -> Dict:
        """Return status dict. Tries squeue (active jobs) then sacct (history)."""
        sq = subprocess.run(
            ["squeue", "-j", job_id, "-h", "-o", "%T|%S|%e"],
            capture_output=True, text=True,
        )
        line = sq.stdout.strip()
        if line:
            parts = line.split("|")
            return {
                "state":      parts[0] if parts else "UNKNOWN",
                "exit_code":  None,
                "start_time": parts[1] if len(parts) > 1 and parts[1] != "N/A" else None,
                "end_time":   parts[2] if len(parts) > 2 and parts[2] != "N/A" else None,
            }

        sa = subprocess.run(
            ["sacct", "-j", job_id, "--format=State,ExitCode,Start,End", "--noheader", "-P"],
            capture_output=True, text=True,
        )
        for raw in sa.stdout.strip().splitlines():
            parts = raw.split("|")
            if len(parts) < 4:
                continue
            state, exit_raw, start, end = parts[:4]
            code_str = exit_raw.split(":")[0] if exit_raw else None
            exit_code = int(code_str) if code_str and code_str.isdigit() else None
            return {
                "state":      state.strip(),
                "exit_code":  exit_code,
                "start_time": start.strip() or None,
                "end_time":   end.strip()   or None,
            }

        raise JobNotFoundError(f"Job {job_id} not found in squeue or sacct")

    def get_job_logs(self, job_id: str) -> str:
        """Return stdout log content for the job."""
        default_log = self.working_dir / f"{job_id}.out"
        if default_log.exists():
            return default_log.read_text()

        # Ask sacct where Slurm wrote the log file
        sa = subprocess.run(
            ["sacct", "-j", job_id, "--format=StdOut", "--noheader", "-P"],
            capture_output=True, text=True,
        )
        for line in sa.stdout.strip().splitlines():
            path = line.strip()
            if path and Path(path).exists():
                return Path(path).read_text()

        return f"[SlurmAdapter] No log found for job {job_id}"

    def wait_until_complete(self, job_id: str, poll_interval: int = 10) -> str:
        """Poll until job reaches a terminal state. Returns log path."""
        while True:
            try:
                status = self.get_job_status(job_id)
            except JobNotFoundError:
                break
            state = status.get("state", "UNKNOWN")
            print(f"[Slurm] job {job_id} → {state}", flush=True)
            if state in _TERMINAL_STATES or state == "UNKNOWN":
                break
            time.sleep(poll_interval)

        return str(self.working_dir / f"{job_id}.out")


# ── Factory ────────────────────────────────────────────────────────────────────

def get_slurm_adapter():
    """Return MockSlurmAdapter or RealSlurmAdapter per EXAMLOPS_SLURM_MODE."""
    _here = Path(__file__).parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))
    from mock_slurm_adapter import MockSlurmAdapter  # noqa: PLC0415

    mode = os.getenv("EXAMLOPS_SLURM_MODE", "mock").lower().strip()
    if mode == "slurm":
        print("[SlurmAdapter] mode=slurm (real HPC)", flush=True)
        return RealSlurmAdapter()
    print("[SlurmAdapter] mode=mock (local)", flush=True)
    return MockSlurmAdapter()
