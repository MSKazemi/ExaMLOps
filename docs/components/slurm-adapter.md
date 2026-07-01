# HPC Scheduler Adapter — Slurm / Flux Abstraction

The scheduler adapter decouples training logic from the compute environment. The same
Prefect pipeline runs on a laptop (mock mode), a Slurm cluster, or a Flux cluster — by
setting environment variables. Two concerns are independent:

- **Scheduler backend** — which queue runs the job: `mock` | `slurm` | `flux`.
- **Transport** — how commands reach the cluster: `local` (subprocess, shared filesystem)
  or `ssh` (paramiko + SFTP, no shared filesystem — a Docker worker submitting to a remote
  login node).

> Backwards compatible: `EXAMLOPS_SLURM_MODE=mock|slurm` still works. `EXAMLOPS_HPC_SCHEDULER`
> takes precedence when set.

## Modes

| Scheduler | Env | Behaviour |
|---|---|---|
| **Mock** (default) | `EXAMLOPS_HPC_SCHEDULER=mock` (or `EXAMLOPS_SLURM_MODE=mock`) | Trains inline in the Prefect worker. No HPC needed. |
| **Slurm** | `EXAMLOPS_HPC_SCHEDULER=slurm` (or `EXAMLOPS_SLURM_MODE=slurm`) | Submits an `sbatch` job. |
| **Flux** | `EXAMLOPS_HPC_SCHEDULER=flux` | Submits a `flux batch` job (flux-core). |

| Transport | Env | Behaviour |
|---|---|---|
| **Local** | `EXAMLOPS_HPC_TRANSPORT=local` | `subprocess` + `shutil.copy`; assumes the worker is on the cluster with a shared FS. |
| **SSH** | `EXAMLOPS_HPC_TRANSPORT=ssh` | paramiko SSH + SFTP; stages the job script up and fetches the model back. |

## Switching modes

```bash
# Mock (default — local development)
exa pipeline run --dummy

# Real Flux over SSH (e.g. the remote cluster; CPU-only, 0 GPUs enrolled)
export EXAMLOPS_HPC_SCHEDULER=flux EXAMLOPS_HPC_TRANSPORT=ssh \
       EXAMLOPS_HPC_SSH_HOST=remote-cpu01 EXAMLOPS_HPC_SSH_USER=<user> \
       EXAMLOPS_HPC_REMOTE_REPO=/path/to/deployed/ExaMLOps EXAMLOPS_HPC_GPUS=0
exa pipeline run --model JPCP --dataset PM100Dataset --dummy

# Real Slurm (worker on a login node, shared FS)
EXAMLOPS_SLURM_MODE=slurm exa pipeline run --dummy
```

## How it works

**Mock** — `slurm_submit_task` runs `model.train_step(loader)` inline, dumps the estimator
to a temp `model.pkl`, and `slurm_wait_task` returns `COMPLETED` immediately.

**Real (slurm | flux)**:

```
slurm_submit_task (real)
  1. get_scheduler_adapter() → FluxAdapter | RealSlurmAdapter (with a RemoteExecutor)
  2. Generates a portable run.sh (no #SBATCH comments — all directives via CLI flags)
     that runs the remote python + slurm_train_script.py, writing <remote_dir>/model.pkl
  3. adapter.submit_job(script, resources, remote_dir=<per-run dir>)
       flux:  flux batch -N.. -n.. -c.. -t..  <staged run.sh>   → F58 id (ƒAbCdEf)
       slurm: sbatch --nodes.. --time.. ..    <staged run.sh>   → numeric id
  4. Records an hpc_jobs tracking row; returns (job_id, <remote_dir>/model.pkl)

slurm_wait_task (real)
  1. adapter.wait_until_complete(job_id) — bounded poll (deadline, UNKNOWN-streak, CLI-timeout)
       flux:  flux jobs / flux job info eventlog
       slurm: squeue (active) → sacct (history)
  2. Updates the hpc_jobs row; raises unless state == COMPLETED
  3. adapter.executor.get(<remote_dir>/model.pkl → local temp)  (a plain copy when local)
  4. Returns (state, local model.pkl)
```

The trained-job id is tagged onto the MLflow run (`hpc_job_id`, `hpc_scheduler`, and the
legacy `slurm_job_id`), so `exa models cost --record` can attribute GPU/CPU-hours.

## Resource configuration

Pass a scheduler-neutral `resources` dict to `submit_job()`; each backend translates:

| Key | sbatch flag | flux flag |
|---|---|---|
| `partition` | `--partition` | — |
| `qos` | `--qos` | `--queue` |
| `account` | `--account` | `--bank` (flux-accounting) |
| `constraint` | `--constraint` | `--requires` |
| `time` | `--time` | `-t<FSD>` (e.g. `2:00:00`→`7200s`) |
| `nodes` | `--nodes` | `-N` |
| `ntasks` | `--ntasks` | `-n` |
| `cpus_per_task` | `--cpus-per-task` | `-c` |
| `mem` | `--mem` | *dropped* (flux-core has no schedulable mem) |
| `gpus` | `--gpus` | `-g` (omitted when `0`) |
| `job_name` | `--job-name` | `--job-name` |

In the pipeline these come from `EXAMLOPS_HPC_*` (falling back to `EXAMLOPS_SLURM_*`); see
the [env-vars reference](../reference/env-vars.md#hpc-scheduler-adapter-phase-23).

## Using the adapter directly

```python
from adapter import get_scheduler_adapter          # respects EXAMLOPS_HPC_SCHEDULER
from executor import SSHExecutor
from flux_adapter import FluxAdapter

adapter = get_scheduler_adapter()                   # factory (mock/slurm/flux + transport)

# Or construct explicitly:
adapter = FluxAdapter(executor=SSHExecutor(host="remote-cpu01", user="me"),
                      remote_workdir="/home/me/examlops_jobs")

job_id = adapter.submit_job("run.sh", resources={"nodes": 2, "time": "2:00:00"},
                            remote_dir="/home/me/examlops_jobs/abc")
status = adapter.get_job_status(job_id)             # {state, exit_code, start_time, end_time}
log_path = adapter.wait_until_complete(job_id, poll_interval=10)
logs = adapter.get_job_logs(job_id)
```

## Job status values

Both backends normalize to the same set. Flux states (`DEPEND/PRIORITY/SCHED`→`PENDING`,
`RUN/CLEANUP`→`RUNNING`, `INACTIVE`+result→terminal) and Slurm states map onto:

| State | Meaning |
|---|---|
| `PENDING` | In queue, not yet running |
| `RUNNING` | Currently executing |
| `COMPLETED` | Finished successfully |
| `FAILED` | Exited with non-zero code |
| `CANCELLED` | Cancelled |
| `TIMEOUT` | Hit the wall-time limit |

Terminal states (polling stops): `COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`.

## Errors

| Exception | When |
|---|---|
| `JobSubmissionError` | Scheduler returned non-zero, or `script_path` not found |
| `JobNotFoundError` | Job id not observable (queue/history/eventlog) |
| `JobTimeoutError` | A scheduler CLI call or the overall wait exceeded its budget |
| `SchedulerAdapterError` (alias `SlurmAdapterError`) | Base class — catch for any failure |

## Notes / limitations

- The training script imports the full modelzoo registry, so the **repo + venv must be
  deployed on the cluster** (per the remote deploy runbook); the worker stages only `run.sh`
  up and fetches `model.pkl` back over SFTP.
- remote Flux currently has **0 GPUs enrolled** — runs are CPU-only; `hpc_jobs.gpus` records 0
  and cost uses a CPU-hour term (`CPU_COST_PER_HOUR`).
- Flux `account`/`qos` require flux-accounting; without it those flags should be left unset.
