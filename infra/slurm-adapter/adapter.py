"""
Dual-mode Slurm adapter factory for ExaMLOps.

Modes:
- mock  : local fake Slurm (for dev/testing)
- slurm : real Slurm integration (for HPC environments)

Mode is selected via environment variable:
  EXAMLOPS_SLURM_MODE=mock|slurm  (default: mock)
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from mock_slurm_adapter import MockSlurmAdapter


class RealSlurmAdapter:
    """
    Minimal real Slurm adapter skeleton.

    NOTE:
    - This is a starting point.
    - Adapt sbatch/squeue/sacct commands to the target HPC site.
    """

    def __init__(self, working_dir: str = "slurm_jobs"):
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(exist_ok=True)

    def submit_job(
        self,
        script_path: Optional[str] = None,
        resources: Optional[Dict] = None,
        training_data: Optional[Dict] = None,
    ) -> str:
        """
        Submit a Slurm job using sbatch.

        script_path: path to a job script (.sh) that will run training.
        resources: optional dict with partition, nodes, gpus, time, etc.
        """
        if not script_path:
            raise ValueError("script_path is required for real Slurm mode")

        script_path = Path(script_path)

        if not script_path.exists():
            raise FileNotFoundError(f"Job script not found: {script_path}")

        cmd = ["sbatch", str(script_path)]

        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(script_path.parent),
        )

        if result.returncode != 0:
            raise RuntimeError(f"sbatch failed: {result.stderr}")

        output = result.stdout.strip()
        job_id = output.split()[-1]
        return job_id

    def get_job_status(self, job_id: str) -> Dict:
        """
        Query Slurm job status using squeue.
        Note: squeue returns empty for completed jobs; use sacct for history.
        """
        cmd = ["squeue", "-j", job_id, "-h", "-o", "%T"]
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return {"state": "UNKNOWN", "raw": result.stderr.strip()}

        state = result.stdout.strip()
        if not state:
            return {"state": "COMPLETED"}  # Left queue = done
        return {"state": state}

    def wait_until_complete(self, job_id: str, poll_interval: int = 10) -> str:
        """
        Poll Slurm until job leaves RUNNING/PENDING.
        Returns path to model artifact (convention: working_dir/job_id/trained_model.pkl).
        """
        while True:
            status = self.get_job_status(job_id)
            state = status.get("state", "UNKNOWN")

            print(f"[Slurm] job {job_id} state: {state}")

            if state in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "UNKNOWN"):
                break

            time.sleep(poll_interval)

        model_path = self.working_dir / job_id / "trained_model.pkl"
        return str(model_path)


def get_slurm_adapter():
    """
    Factory that returns MockSlurmAdapter or RealSlurmAdapter
    depending on EXAMLOPS_SLURM_MODE.
    """
    mode = os.getenv("EXAMLOPS_SLURM_MODE", "mock").lower().strip()

    if mode == "slurm":
        print("[SlurmAdapter] Using REAL Slurm adapter (mode=slurm)")
        return RealSlurmAdapter()
    else:
        print("[SlurmAdapter] Using MOCK Slurm adapter (mode=mock)")
        return MockSlurmAdapter()
