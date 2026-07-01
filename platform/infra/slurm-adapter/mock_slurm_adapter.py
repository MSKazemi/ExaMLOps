"""
Mock Slurm Adapter

Simulates HPC job submission and execution locally — no Slurm required.
Implements the same interface as RealSlurmAdapter (api.md FR-HY-02/03/04).

Two execution modes:
  - training_data provided → runs real sklearn training, saves model.pkl
  - training_data=None     → executes the script_path as a subprocess (local bash)
"""

from __future__ import annotations

import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
from sklearn.ensemble import RandomForestRegressor


class MockSlurmAdapter:
    """Simulates Slurm job submission and monitoring locally."""

    def __init__(self, working_dir: str | None = None, executor=None):
        # ``executor`` is accepted for interface parity with the real adapters and ignored.
        if working_dir is None:
            working_dir = Path(__file__).parent / "mock_hpc_jobs"
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = {}

    # ── Public interface ───────────────────────────────────────────────────────

    def submit_job(
        self,
        script_path: str | None = None,
        resources: dict | None = None,
        training_data: dict[str, Any] | None = None,
        remote_dir: str | None = None,  # ignored; interface parity
    ) -> str:
        """
        Submit a job (simulated). Returns a UUID job ID immediately.

        script_path:   optional path to a shell script to run (if no training_data)
        resources:     ignored in mock mode (accepted for interface compatibility)
        training_data: dict with X_train / y_train arrays → real sklearn training
        """
        job_id = str(uuid.uuid4())
        job_folder = self.working_dir / job_id
        job_folder.mkdir(parents=True, exist_ok=True)

        self._jobs[job_id] = {
            "state": "PENDING",
            "folder": job_folder,
            "script_path": script_path,
            "training_data": training_data,
            "resources": resources or {},
            "exit_code": None,
            "start_time": None,
            "end_time": None,
            "log_lines": [],
        }
        print(f"[MockSlurm] Submitted job {job_id}", flush=True)
        return job_id

    def wait_until_complete(
        self,
        job_id: str,
        poll_interval: int = 0,
        max_wait_s: int | None = None,
        sleep_time: int | None = None,
    ) -> str:
        """
        Execute the job synchronously and return the path to the trained model (or log).

        ``poll_interval`` (and the deprecated ``sleep_time`` alias) is used only as an
        optional artificial delay for the no-op path. ``max_wait_s`` is accepted for
        interface parity with the real adapters and ignored.

        Returns: path to trained_model.pkl if training succeeded, else path to stdout log.
        """
        if sleep_time is not None:
            poll_interval = sleep_time
        job = self._get_job(job_id)
        job["state"] = "RUNNING"
        job["start_time"] = _now()
        print(f"[MockSlurm] Running job {job_id} ...", flush=True)

        job_folder: Path = job["folder"]
        model_file = job_folder / "trained_model.pkl"
        log_file = job_folder / "stdout.log"
        log_lines = job["log_lines"]

        try:
            training_data = job.get("training_data")
            if training_data is not None:
                artifact_path = self._run_training(training_data, model_file, log_lines)
            elif job.get("script_path"):
                artifact_path = self._run_script(job["script_path"], job_folder, log_lines)
            else:
                # Neither training_data nor script — produce a placeholder
                if poll_interval:
                    time.sleep(poll_interval)
                log_lines.append("[MockSlurm] No-op job (no training_data or script_path)")
                artifact_path = str(job_folder / "no_artifact")

            job["state"] = "COMPLETED"
            job["exit_code"] = 0
        except Exception as exc:
            log_lines.append(f"[MockSlurm] Job failed: {exc}")
            job["state"] = "FAILED"
            job["exit_code"] = 1
            artifact_path = str(log_file)

        job["end_time"] = _now()
        log_file.write_text("\n".join(log_lines))
        print(f"[MockSlurm] Job {job_id} → {job['state']}", flush=True)
        return artifact_path

    def get_job_status(self, job_id: str) -> dict:
        """
        Return status dict matching the RealSlurmAdapter interface:
          {state, exit_code, start_time, end_time}
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"state": "UNKNOWN", "exit_code": None, "start_time": None, "end_time": None}
        return {
            "state": job["state"],
            "exit_code": job["exit_code"],
            "start_time": job["start_time"],
            "end_time": job["end_time"],
        }

    def get_job_logs(self, job_id: str) -> str:
        """Return captured stdout for the job."""
        job = self._jobs.get(job_id)
        if job is None:
            return f"[MockSlurm] Unknown job {job_id}"

        log_file = job["folder"] / "stdout.log"
        if log_file.exists():
            return log_file.read_text()

        lines = job.get("log_lines", [])
        return "\n".join(lines) if lines else f"[MockSlurm] No logs yet for job {job_id}"

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get_job(self, job_id: str) -> dict:
        from adapter import JobNotFoundError  # noqa: PLC0415

        if job_id not in self._jobs:
            raise JobNotFoundError(f"Unknown job_id: {job_id}")
        return self._jobs[job_id]

    @staticmethod
    def _run_training(training_data: dict, model_file: Path, log_lines: list) -> str:
        """Train a RandomForestRegressor on the supplied data and save to model_file."""
        import numpy as np

        X_train = np.asarray(training_data["X_train"])
        y_train = np.asarray(training_data["y_train"])
        log_lines.append(f"[MockSlurm] Training on {X_train.shape[0]} samples ...")

        model = RandomForestRegressor(n_estimators=50, max_depth=8, random_state=42)
        model.fit(X_train, y_train)
        joblib.dump(model, model_file)
        log_lines.append(f"[MockSlurm] Model saved → {model_file}")
        return str(model_file)

    @staticmethod
    def _run_script(script_path: str, job_folder: Path, log_lines: list) -> str:
        """Execute a shell script locally and capture its output."""
        result = subprocess.run(
            ["bash", script_path],
            capture_output=True,
            text=True,
            cwd=str(job_folder),
        )
        log_lines.extend(result.stdout.splitlines())
        if result.stderr:
            log_lines.extend(result.stderr.splitlines())
        if result.returncode != 0:
            raise RuntimeError(f"Script exited with code {result.returncode}")
        return str(job_folder / "trained_model.pkl")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
