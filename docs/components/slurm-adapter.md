# Slurm Adapter — HPC Abstraction

The Slurm adapter decouples training logic from the compute environment. The same Prefect pipeline runs on a laptop (mock mode) or a real HPC cluster (slurm mode) — just by setting one environment variable.

## Modes

| Mode | Env var value | Behaviour |
|---|---|---|
| **Mock** (default) | `EXAMLOPS_SLURM_MODE=mock` | Trains inline in the Prefect worker process. No HPC needed. |
| **Real Slurm** | `EXAMLOPS_SLURM_MODE=slurm` | Submits an `sbatch` job to the cluster. Requires the HPC tools to be on PATH. |

## Switching modes

```bash
# Mock (default — use this for local development)
exa pipeline run --dummy

# Real Slurm
EXAMLOPS_SLURM_MODE=slurm exa pipeline run --dummy
```

## Mock mode — how it works

In mock mode, the `slurm_submit_task` runs `model.train_step(loader)` inline:

```
slurm_submit_task (mock)
  1. Calls model.train_step(loader) — trains the sklearn estimator in-process
  2. Serialises the estimator to a temp directory as model.pkl
  3. Returns (job_id="mock-<uuid>", artifact_path="/tmp/examlops_mock_.../model.pkl")

slurm_wait_task (mock)
  1. artifact_path is already set → returns "COMPLETED" immediately
```

This means the full pipeline completes in seconds with no HPC dependency.

## Real Slurm mode — how it works

In real mode, the adapter calls `sbatch` to submit a pre-baked training script:

```
slurm_submit_task (real)
  1. Validates RealSlurmAdapter is reachable
  2. Submits script via sbatch with resource flags
  3. Returns (job_id="12345678", artifact_path=None)

slurm_wait_task (real)
  1. Polls squeue (active jobs) every 10 seconds
  2. Falls back to sacct (history) once job leaves the queue
  3. Waits until state ∈ {COMPLETED, FAILED, CANCELLED, TIMEOUT}
  4. Returns (state, "{working_dir}/{job_id}/model.pkl")
```

## Resource configuration

Pass a `resources` dict to `submit_job()` to override sbatch flags:

```python
resources = {
    "partition":     "gpu",
    "time":          "02:00:00",
    "nodes":         1,
    "ntasks":        8,
    "cpus_per_task": 4,
    "mem":           "32G",
    "gpus":          "1",
    "job_name":      "examlops_train",
}
adapter.submit_job(script_path="train.sh", resources=resources)
```

Supported keys map directly to sbatch flags:

| Key | sbatch flag |
|---|---|
| `partition` | `--partition` |
| `time` | `--time` |
| `nodes` | `--nodes` |
| `ntasks` | `--ntasks` |
| `cpus_per_task` | `--cpus-per-task` |
| `mem` | `--mem` |
| `gpus` | `--gpus` |
| `output` | `--output` |
| `error` | `--error` |
| `job_name` | `--job-name` |
| `account` | `--account` |

Default log locations are `{working_dir}/{job_id}.out` and `{working_dir}/{job_id}.err`.

## Using the adapter directly

```python
from adapter import get_slurm_adapter, RealSlurmAdapter

# Factory — respects EXAMLOPS_SLURM_MODE
adapter = get_slurm_adapter()

# Direct instantiation
adapter = RealSlurmAdapter(working_dir="slurm_jobs")

# Submit
job_id = adapter.submit_job("train.sh", resources={"partition": "cpu"})

# Status
status = adapter.get_job_status(job_id)
# {"state": "RUNNING", "exit_code": None, "start_time": "...", "end_time": None}

# Wait (blocking)
log_path = adapter.wait_until_complete(job_id, poll_interval=10)

# Logs
logs = adapter.get_job_logs(job_id)
```

## Job status values

The adapter maps Slurm states directly:

| State | Meaning |
|---|---|
| `PENDING` | In queue, not yet running |
| `RUNNING` | Currently executing |
| `COMPLETED` | Finished successfully |
| `FAILED` | Exited with non-zero code |
| `CANCELLED` | Manually cancelled |
| `TIMEOUT` | Hit the wall time limit |

Terminal states (adapter stops polling): `COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`.

## Errors

| Exception | When |
|---|---|
| `JobSubmissionError` | `sbatch` returned non-zero, or `script_path` not found |
| `JobNotFoundError` | Job ID not found in `squeue` or `sacct` |
| `SlurmAdapterError` | Base class — catch this for any adapter failure |

## Current limitation

Real Slurm mode requires a pre-baked training script that saves the trained estimator to `{working_dir}/{job_id}/model.pkl` on the shared filesystem. The automatic generation of this script from a `SeanergysModel` instance is not yet implemented. Use `EXAMLOPS_SLURM_MODE=mock` for local development.
