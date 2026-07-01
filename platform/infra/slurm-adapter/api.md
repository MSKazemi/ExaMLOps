# HPC Scheduler Adapter API

Python interface for the scheduler adapters. Implements FR-HY-02, FR-HY-03, FR-HY-04.

Two orthogonal axes (see `../../../design/adr/0002-hpc-scheduler-abstraction.md`):
scheduler backend (`mock`/`slurm`/`flux`) and transport (`local`/`ssh`).

## Implementation Status

- **Contract:** `scheduler.py` — `SchedulerAdapter` Protocol, `JobStatus`, exceptions,
  and `BasePollingAdapter` (shared hardened `wait_until_complete`).
- **Transport:** `executor.py` — `RemoteExecutor` Protocol, `LocalExecutor`,
  `SSHExecutor` (paramiko + SFTP), `get_executor()`.
- **Mock:** `mock_slurm_adapter.py` — simulates jobs locally for dev/test.
- **Slurm:** `adapter.py` → `RealSlurmAdapter` — `sbatch`/`squeue`/`sacct` via an executor.
- **Flux:** `flux_adapter.py` → `FluxAdapter` — `flux batch`/`flux jobs`/`flux job info`.
- **Factory:** `get_scheduler_adapter()` in `adapter.py` — selects backend via
  `EXAMLOPS_HPC_SCHEDULER=mock|slurm|flux` (legacy `EXAMLOPS_SLURM_MODE` honored).
  `get_slurm_adapter()` remains as an alias.

All adapters implement the same 4 methods below. `submit_job` also accepts an optional
`remote_dir` (per-run directory where the job writes `model.pkl`, fetched back via the
executor). Exceptions: `SchedulerAdapterError` (alias `SlurmAdapterError`),
`JobSubmissionError`, `JobNotFoundError`, `JobTimeoutError`.

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