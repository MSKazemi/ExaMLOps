"""
Mock Slurm Adapter

Simulates HPC job submission and execution locally.
Used for development and testing without Slurm access.

In mock mode with training_data: runs real sklearn training and saves model.
Implements the interface defined in api.md (FR-HY-02, FR-HY-03, FR-HY-04).
"""

import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import joblib
from sklearn.ensemble import RandomForestRegressor


class MockSlurmAdapter:
    """Simulates Slurm job submission and monitoring."""

    def __init__(self, working_dir=None):
        if working_dir is None:
            working_dir = Path(__file__).parent / "mock_hpc_jobs"
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = {}

    def submit_job(
        self,
        script_path: Optional[str] = None,
        resources: Optional[Dict] = None,
        training_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit a job (simulated). Stores training_data for real training in wait."""
        job_id = str(uuid.uuid4())
        job_folder = self.working_dir / job_id
        job_folder.mkdir()

        self.jobs[job_id] = {
            "state": "PENDING",
            "folder": job_folder,
            "training_data": training_data,
        }
        return job_id

    def wait_until_complete(self, job_id: str, sleep_time: int = 1) -> str:
        """
        Wait for job to complete.
        If training_data was provided: train real model, save pickle, return path.
        Else: create dummy artifact (legacy).
        """
        if job_id not in self.jobs:
            raise ValueError(f"Unknown job_id: {job_id}")

        print(f"[MockSlurm] Running job {job_id}...")
        self.jobs[job_id]["state"] = "RUNNING"

        job_folder = self.jobs[job_id]["folder"]
        model_file = job_folder / "trained_model.pkl"

        training_data = self.jobs[job_id].get("training_data")
        if training_data is not None:
            # Real training
            import numpy as np

            X_train = np.asarray(training_data["X_train"])
            y_train = np.asarray(training_data["y_train"])

            model = RandomForestRegressor(n_estimators=50, max_depth=8, random_state=42)
            model.fit(X_train, y_train)
            joblib.dump(model, model_file)
            print(f"[MockSlurm] Trained model saved to {model_file}")
        else:
            time.sleep(sleep_time)
            model_file.write_text("dummy model artifact")

        self.jobs[job_id]["state"] = "COMPLETED"
        return str(model_file)

    def get_job_status(self, job_id: str) -> Dict:
        """Get job status."""
        return self.jobs.get(job_id, {"state": "UNKNOWN"})
