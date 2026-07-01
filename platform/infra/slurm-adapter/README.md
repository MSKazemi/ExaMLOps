# Slurm Adapter

Abstraction between the MLOps workflow engine and HPC clusters. Implements FR-HY-02, FR-HY-03, FR-HY-04.

## Contents

| File | Purpose |
|------|---------|
| [api.md](api.md) | Python interface specification |
| [adapter.py](adapter.py) | Dual-mode factory (`get_slurm_adapter()`) |
| [mock_slurm_adapter.py](mock_slurm_adapter.py) | Mock implementation for local dev |

## Modes

The Slurm adapter supports two modes:

| Mode | Use case |
|------|----------|
| `mock` (default) | Local dev/testing — fake job execution, no Slurm |
| `slurm` | HPC — real `sbatch` / `squeue` integration |

Mode is selected via environment variable:

```bash
export EXAMLOPS_SLURM_MODE=mock   # default
export EXAMLOPS_SLURM_MODE=slurm  # for HPC
```

The UC power training flow uses the factory:

```python
from adapter import get_slurm_adapter
adapter = get_slurm_adapter()
job_id = adapter.submit_job(script_path="train_job.sh", resources=None)
model_path = adapter.wait_until_complete(job_id)
```

## Mock Mode (Local Development)

- `submit_job()` — Returns a fake job ID (ignores script_path)
- `wait_until_complete(job_id)` — Sleeps ~2s, creates dummy `trained_model.pkl`
- `get_job_status(job_id)` — Returns job state

Output: `infra/slurm-adapter/mock_hpc_jobs/<job_id>/`

## Slurm Mode (HPC)

- `submit_job(script_path, resources)` — Runs `sbatch script_path`
- `get_job_status(job_id)` — Runs `squeue -j job_id`
- `wait_until_complete(job_id)` — Polls until job leaves queue

Requires a real `train_job.sh` (or equivalent) that runs training and writes the model to a known path.
