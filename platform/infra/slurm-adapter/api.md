# Slurm Adapter API

Python interface for the Slurm adapter. Implements FR-HY-02, FR-HY-03, FR-HY-04.

## Implementation Status

- **Mock:** `mock_slurm_adapter.py` — simulates jobs locally for dev/test.
- **Slurm:** `adapter.py` → `RealSlurmAdapter` — real `sbatch`/`squeue` on HPC.
- **Factory:** `get_slurm_adapter()` in `adapter.py` — selects mode via `EXAMLOPS_SLURM_MODE=mock|slurm`.

---

These calls will be used by the workflow engine to:
- Submit training jobs to HPC clusters
- Wait for completion
- Collect logs and exit status

---

## Interface (Proposed)

### `submit_job(script_path: str, resources: dict) -> str`

Submit a job to Slurm. Returns the job ID.

**Parameters:**
- `script_path`: Path to the job script (e.g. `train.sh` or `train.py`)
- `resources`: Dict with keys such as:
  - `partition`: Slurm partition name (optional)
  - `time`: Wall-clock time limit (e.g. `"2:00:00"`)
  - `nodes`: Number of nodes
  - `ntasks`: Number of tasks
  - `cpus_per_task`: CPUs per task
  - `mem`: Memory per node (e.g. `"16G"`)

**Returns:** Job ID string (e.g. `"12345678"`)

---

### `get_job_status(job_id: str) -> dict`

Get the current status of a job.

**Returns:**
```python
{
    "state": "RUNNING" | "PENDING" | "COMPLETED" | "FAILED" | "CANCELLED",
    "exit_code": int | None,  # Present when COMPLETED or FAILED
    "start_time": str | None,
    "end_time": str | None,
}
```

---

### `get_job_logs(job_id: str) -> str`

Retrieve stdout/stderr logs for a completed or running job.

**Returns:** Log content as string

---

## Error Handling

- `SlurmAdapterError`: Base exception for adapter failures
- `JobSubmissionError`: Failed to submit job (e.g. invalid script, queue full)
- `JobNotFoundError`: Job ID does not exist