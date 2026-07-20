# HPC Training Workflow — run a Prefect training flow on a real Slurm/Flux cluster

This guide takes you end-to-end from "the platform trains inline in mock mode" to
"the Prefect `training_flow` submits a real batch job to a Slurm or Flux cluster, waits
for it, fetches the model back, logs it to MLflow, and promotes it."

It complements two neighbours:

- **[hpc-fleet.md](hpc-fleet.md)** — how to *discover, register, and approve* a cluster
  (`exa hpc detect/connect/approve`). Read that first if you want the governed
  `--cluster <name>` path.
- **[../components/slurm-adapter.md](../components/slurm-adapter.md)** — the adapter/transport
  internals (`FluxAdapter`, `SSHExecutor`, resource-flag mapping).

The worked example at the end trains **JPCP × PM100Dataset** on a live **Flux** instance.

> **Two axes, always orthogonal.** A run is defined by a **scheduler backend**
> (`mock` | `slurm` | `flux`) *and* a **transport** (`local` subprocess | `ssh` paramiko).
> The adapter never knows which transport it has, so the same flow works whether the Prefect
> worker runs *on* the login node or drives it remotely over SSH.

---

## 1. How a training run reaches the scheduler

`exa pipeline run` shells out to `pipelines/pipeline_generator.py`, which runs the Prefect
`training_flow`. The flow's 7 tasks are transport-agnostic; only one branches on the backend:

```
data_extraction → slurm_submit → slurm_wait → result_fetch → evaluate → log_mlflow → promote
```

`slurm_submit_task` resolves the backend with `_hpc_scheduler_name()`:

| Backend | What `slurm_submit_task` does |
|---|---|
| `mock` (default) | Trains **inline** in the Prefect worker, dumps `model.pkl` locally. No scheduler. |
| `slurm` / `flux` | Writes a portable `run.sh`, submits it via the scheduler adapter, and `slurm_wait_task` polls to a terminal state then fetches `model.pkl` back. |

In real-HPC mode the generated `run.sh` is deliberately minimal — all resource directives
flow through **CLI flags** (not `#SBATCH` comments), so the identical script works for both
`sbatch` and `flux batch`:

```bash
#!/bin/bash
mkdir -p <remote_dir>
<remote_python> pipelines/slurm_train_script.py \
  --model JPCP --dataset PM100Dataset \
  --output <remote_dir>/model.pkl \
  --mlflow-uri http://localhost:15000
```

`slurm_train_script.py` runs on the compute node: it re-imports the model registry, calls
`get_train_components(...)`, `model.train_step(...)`, and `joblib.dump(estimator, --output)`.

---

## 2. Select the scheduler and transport

Everything is environment-driven. The two variables that pick backend and transport:

```bash
export EXAMLOPS_HPC_SCHEDULER=flux     # mock | slurm | flux   (legacy: EXAMLOPS_SLURM_MODE=slurm)
export EXAMLOPS_HPC_TRANSPORT=local    # local | ssh           (defaults to ssh iff *_SSH_HOST set)
```

### Transport A — worker runs *on* the login node (`local`)

Simplest when your Prefect worker (or your shell) is already on the cluster login node that
can talk to the scheduler. Files are staged with `shutil.copy`; commands run via `subprocess`.

```bash
export EXAMLOPS_HPC_SCHEDULER=flux
export EXAMLOPS_HPC_TRANSPORT=local
```

### Transport B — remote driver over SSH (`ssh`)

Lets a laptop or a Docker Prefect worker submit to a remote login node with **no shared
filesystem**. Uses paramiko + SFTP with a reused connection.

```bash
export EXAMLOPS_HPC_SCHEDULER=flux
export EXAMLOPS_HPC_TRANSPORT=ssh
export EXAMLOPS_HPC_SSH_HOST=lxp-cpu01          # the login node that runs the scheduler
export EXAMLOPS_HPC_SSH_USER=u1002
export EXAMLOPS_HPC_SSH_KEY=~/.ssh/id_ed25519   # optional; SSH agent / default keys also work
export EXAMLOPS_HPC_REMOTE_REPO=/nfs/share01/examlops                 # repo on the cluster
export EXAMLOPS_HPC_REMOTE_PYTHON=/nfs/share01/examlops/.venv/bin/python
export EXAMLOPS_HPC_REMOTE_WORKDIR=/nfs/share01/examlops/flux_jobs    # where job dirs live
```

> **Host-key verification is on by default.** The SSH transport uses paramiko's
> `RejectPolicy` — an unknown host key aborts the connection. Add the node to `known_hosts`
> first (`ssh-keyscan lxp-cpu01 >> ~/.ssh/known_hosts`), or for a trusted first connect set
> `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS=1` (dev only).

---

## 3. Request resources (scheduler-neutral)

`slurm_submit_task` builds one resource dict from `EXAMLOPS_HPC_*` env vars; each adapter maps
it to its own flags. Set only what you need — every value has a default.

| Env var | Default | Slurm flag | Flux flag |
|---|---|---|---|
| `EXAMLOPS_HPC_NODES` | `1` | `--nodes` | `-N` |
| `EXAMLOPS_HPC_NTASKS` | `1` | `--ntasks` | `-n` |
| `EXAMLOPS_HPC_CPUS` | `4` | `--cpus-per-task` | `-c` |
| `EXAMLOPS_HPC_GPUS` | *(unset)* | `--gpus` | `-g` (emitted only when > 0) |
| `EXAMLOPS_HPC_TIME` | `2:00:00` | `--time` | `-t<seconds>` |
| `EXAMLOPS_HPC_PARTITION` | *(unset)* | `--partition` | *(n/a)* |
| `EXAMLOPS_HPC_QOS` | *(unset)* | `--qos` | `--queue` |
| `EXAMLOPS_HPC_ACCOUNT` | *(unset)* | `--account` | `--bank` *(flux-accounting)* |
| `EXAMLOPS_HPC_CONSTRAINT` | *(unset)* | `--constraint` | `--requires` |
| `EXAMLOPS_HPC_MEM` | `16G` | `--mem` | *dropped — flux-core has no schedulable memory* |

```bash
export EXAMLOPS_HPC_NODES=1
export EXAMLOPS_HPC_CPUS=8
export EXAMLOPS_HPC_TIME=1:00:00
# export EXAMLOPS_HPC_GPUS=1          # only on a GPU-enabled instance
```

---

## 4. Run the flow

With the environment set, the ordinary command trains on the scheduler — no extra flags:

```bash
exa pipeline run --model JPCP --dataset PM100Dataset
```

Watch for these lines — they confirm the real-HPC path (not mock):

```
[scheduler] backend=flux (real HPC)
[slurm_submit] flux job_id=ƒCw8cWS4fw5  remote_dir=/nfs/share01/examlops/flux_jobs/42434e6c3c24
[scheduler] job ƒCw8cWS4fw5 → RUNNING
...
[scheduler] job ƒCw8cWS4fw5 → COMPLETED
```

> **Full data vs. smoke test.** Without `--dummy`, a real-HPC run trains on the **full**
> dataset (JPCP × PM100 is ~142k rows → several minutes of RandomForest). For a fast
> end-to-end smoke test on the scheduler, add `--dummy` — it is forwarded to the compute
> node and trains on the small dummy split (~50 rows, seconds):
>
> ```bash
> exa pipeline run --model JPCP --dataset PM100Dataset --dummy
> ```

The flow finishes by logging the run to MLflow and, if the metric passes the model's
lifecycle threshold, setting its `@Production` alias. Afterwards:

```bash
exa serve reload        # roll the new Production alias into Ray Serve
exa hpc jobs --model JPCP   # the run is recorded in the hpc_jobs table (scheduler, nodes, cpus, exit_code)
```

---

## 5. Governed path — `exa hpc` fleet registry

Instead of exporting `EXAMLOPS_HPC_*` by hand, register the cluster once and target it by
name. `--cluster` refuses any cluster that a sysadmin has not **approved** (`ACTIVE`), and
resolves the same env for you. See **[hpc-fleet.md](hpc-fleet.md)** for the full flow.

```bash
exa hpc connect lxp-cpu01 --name lxp --user u1002    # → clusters.yaml + PENDING row
exa hpc approve lxp                                   # sysadmin gate → ACTIVE
exa hpc preflight lxp                                 # exits 1 if the cluster can't take the job
exa pipeline run --model JPCP --dataset PM100Dataset --cluster lxp
exa pipeline run --model JPCP --dataset PM100Dataset --cluster auto --gpus 1   # let placement choose
```

---

## 6. Worked example — JPCP on the live Flux instance (`lxp`)

The `lxp` deployment runs **flux-core 0.85.0** on a 2-node instance
(`seanergys-lxp-cpu[01-02]`, 32 cores, 0 GPUs), with `flux-accounting` and `flux-sched`
available. `flux` is on the system `PATH`, so no `module load` is needed.

> Use SSH host **`lxp-cpu01`**, not `lxp` — the `lxp` alias forces an interactive
> `RemoteCommand` and cannot take a non-interactive command.

Running the Prefect worker directly on the login node (`local` transport):

```bash
ssh lxp-cpu01
cd /nfs/share01/examlops

export EXAMLOPS_HPC_SCHEDULER=flux
export EXAMLOPS_HPC_TRANSPORT=local
export MLFLOW_TRACKING_URI=http://localhost:15000

exa pipeline run --model JPCP --dataset PM100Dataset --dummy   # fast smoke test
```

Verify the batch job independently with the native Flux CLI:

```bash
flux resource list                       # 2 nodes / 32 cores free
flux jobs -a -o '{id} {state} {result} {runtime}'   # your job → INACTIVE COMPLETED
```

A verified full-data run on this instance produced: Flux job `ƒCw8cWS4fw5`, scheduled on
**1 node / 4 cores**, trained the JPCP RandomForest on **142,378** PM100 samples in **329s**,
saved a **20 MB `model.pkl`**, and the job reached `COMPLETED`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Cannot execute command-line and remote command` over SSH | You used the `lxp` host alias (forced `RemoteCommand`). Use `lxp-cpu01`. |
| `flux: command not found` over SSH | The remote non-login shell has no `flux` on `PATH`. Ensure the flux install dir is on the system `PATH` (`/etc/profile.d`), or wrap the remote in a login shell. |
| Job stays `RUNNING` for minutes | Expected without `--dummy` — the compute node imports the full stack and trains on the full dataset. Add `--dummy` for a seconds-long smoke test. |
| `paramiko … RejectPolicy` / host-key error | Add the node to `known_hosts`, or set `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS=1` for a trusted first connect. |
| `exa pipeline run --cluster X` refuses to run | `X` is not `ACTIVE`. Approve it: `exa hpc approve X` (sysadmin gate). |
| GPU job never schedules | The target instance has 0 GPUs (`flux resource list`). Point at a GPU-enabled cluster or drop `EXAMLOPS_HPC_GPUS`. |

---

## Related

- [hpc-fleet.md](hpc-fleet.md) — discover, register, and approve clusters (`exa hpc`)
- [../components/slurm-adapter.md](../components/slurm-adapter.md) — adapter & transport internals
- [../components/prefect.md](../components/prefect.md) — the `training_flow` task graph
- [../reference/env-vars.md](../reference/env-vars.md) — full HPC env-var reference
- [gpu-sharing.md](gpu-sharing.md) — fractional-GPU scheduling
