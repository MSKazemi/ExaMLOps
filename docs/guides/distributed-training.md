# Distributed & Fault-Tolerant Training (E6)

> Next-Gen 40 · feature **E6** · ADR 0032 · spec `design/vision/specs/E6-distributed-fault-tolerant-training.md`

E6 runs training across **multiple processes and nodes** (PyTorch DDP or FSDP2; DeepSpeed ZeRO and
Megatron through a model's own entrypoint) through the phase-23 scheduler abstraction, and —
critically for long HPC jobs that get preempted — **checkpoints periodically, keeps a durable copy,
and auto-resumes** from the last valid checkpoint instead of restarting from scratch.

There are three ways in:

| Command | What it does |
|---|---|
| `exa pipeline run --model M --distributed` | The platform path: the model YAML's `distributed:` block → scheduler job → resubmit/resume on failure → cost, lineage, MLflow link. |
| `exa pipeline distributed run --scheduler --model M` | The same, with flags that override the YAML (`--strategy`, `--nodes`, `--min-nodes`, `--checkpoint-store`, …). |
| `exa pipeline distributed run --local` | Real `torchrun` on this machine, supervised locally (no scheduler). |

`exa pipeline distributed launch` only *records* a planned run and prints the `torchrun` command it
would use (the model's entrypoint, or the shipped reference script — no placeholder).

## Per-model plan: the `distributed:` block

The model YAML (single source of truth, Phase 14) selects strategy, topology, elasticity and NCCL
configuration:

```yaml
name: JPCP
distributed:
  strategy: fsdp            # ddp | fsdp (default) | zero | megatron
  nodes: 4                  # maximum nodes
  min_nodes: 2              # < nodes ⇒ torch elastic: --nnodes=2:4, re-rendezvous on node loss
  gpus_per_node: 4          # 0 = CPU (gloo)
  nproc_per_node: 4         # default: gpus_per_node, else 1
  max_restarts: 2           # torchrun in-job restarts before the job fails
  max_attempts: 3           # scheduler (re)submissions before giving up (1-20)
  steps: 1000
  checkpoint_every: 50
  entrypoint: my_pack.train_dist   # module run with `torchrun -m`; default = reference script
  nccl:                     # exported into every rank's environment
    NCCL_SOCKET_IFNAME: ib0
    NCCL_IB_HCA: mlx5_0:1
```

Unknown keys are errors, not silent no-ops (a typo'd `min-nodes:` must not quietly disable
elasticity). `nccl:` accepts only `NCCL_*` / `TORCH_NCCL_*` names that do not look like secrets,
with values restricted to `[A-Za-z0-9_.,:/=^+-]` — it is configuration that is written into the job
script, never a credential. Flags on `exa pipeline distributed run` override the YAML.

**Preflight (fail fast).** Before anything is submitted the plan is checked: `zero` needs
DeepSpeed and `megatron` needs Megatron-Core installed, and both need an `entrypoint` (the reference
script implements `ddp` and `fsdp` only); the entrypoint must import; NCCL settings on a CPU plan
are refused. A refused plan exits 2 and submits nothing.

**Entrypoint contract.** A model's entrypoint module accepts `--run-dir --steps --checkpoint-every
--seed --strategy`, writes checkpoints in the layout below (use
`examlops.distributed.checkpoint_files`), resumes from `find_latest_valid`, prints the final
`EXAMLOPS_DIST_METRICS=<json>` line and uses exit codes 0 / 75 (recoverable) / 70 (fatal).
`examlops/distributed/train_ddp.py` is the working template.

## Through the scheduler

```bash
EXAMLOPS_HPC_SCHEDULER=slurm exa pipeline run --model JPCP --distributed
exa pipeline distributed run --scheduler --model JPCP --strategy fsdp --nodes 4 --min-nodes 2 \
    --checkpoint-store s3://checkpoints/dist --dataset-revision <A1-rev> --mlflow-run-id <id>
```

Both commands refuse a model with no YAML in the active pack (a typo would otherwise run on the
default plan and record its cost under a model that does not exist). Both run under the admission
gate (ADR 0116) sized on the plan (`nodes × gpus_per_node`), and both consult the project budget
gate. `exa pipeline run --distributed` trains **the model**, so it also requires
`distributed.entrypoint`: without one the job would run the synthetic reference script, and its
cost, lineage and MLflow tags would be recorded as that model's training. Smoke-test a plan on the
reference script with `exa pipeline distributed run --scheduler --model <name>`.

One attempt:

1. **Restore** — with a durable store configured, the newest valid checkpoint newer than the run
   directory's is downloaded and re-verified (see below).
2. **Submit** — a generated `run.sh` (mode 0700, every value shell-quoted) goes to the configured
   adapter (`EXAMLOPS_HPC_SCHEDULER` = `mock` | `slurm` | `flux`, or the cluster `--cluster`
   resolved) with scheduler-neutral resources: `nodes`, one task per node, `gpus_per_node`.
   - one node: `python -m torch.distributed.run --standalone …`
   - Slurm: `HEAD=$(python -m examlops.distributed.rendezvous "$SLURM_JOB_NODELIST")`, then
     `srun --nodes=N --ntasks-per-node=1 … --rdzv_endpoint="$HEAD:29500"`
   - Flux: the same with `flux getattr hostlist` and `flux run -N N -n N`.
   The rendezvous host is the first node of the **actual allocation**, read at run time
   (`EXAMLOPS_DIST_RDZV_PORT` overrides the port). GPU plans export
   `TORCH_NCCL_ASYNC_ERROR_HANDLING=1` unless the site sets it, so a hung collective fails the
   attempt instead of burning the allocation.
3. **Wait and classify** — success needs the job COMPLETED **and** the script's completion line; a
   `FATAL.json` marker means do not resubmit; anything else (including a job that exceeds
   `EXAMLOPS_DIST_MAX_WAIT_S`, which is cancelled) is recoverable.
4. **Mirror and record** — valid checkpoints go to the durable store, are recorded in
   `training_checkpoints` with their durable URI and MLflow run id, and the job in `hpc_jobs`
   (`exa hpc jobs`).
5. **Resubmit** — a recoverable failure is resubmitted **through the scheduler** with exponential
   backoff, honouring the ADR 0109 preemption gate (`EXAMLOPS_SUSPEND_PREEMPTION_GATE`). A refused
   submission (bad partition, closed queue) stops immediately rather than looping.

On completion: **cost** (declared nodes × GPUs — or processes on CPU — × each attempt's wall clock,
failed attempts included) is recorded in `distributed_runs.cost_gpu_hours` and in `model_costs` as
version 0 ("trained, not yet registered"), so `exa models cost <model>` shows it; the **MLflow**
run (if `--mlflow-run-id`) is tagged with `examlops.distributed.run_id`,
`examlops.checkpoint.step/uri` and `hpc_job_id`; an OpenLineage START → COMPLETE/FAIL pair links
the dataset revision → run → model + checkpoint. Launch, submit, failure, resubmit, resume, lost
job, mirror/restore and completion are audited under source `exa-distributed`.

The run directory must be visible to the submitting host (shared filesystem) for a remote Slurm or
Flux cluster; the default is `$EXAMLOPS_DATA_DIR/distributed/<run-id>`.

## Durable checkpoints (NFS / MinIO)

```bash
export EXAMLOPS_DIST_CHECKPOINT_STORE=/nfs/share01/examlops/checkpoints   # or s3://bucket/prefix
```

| Store | Notes |
|---|---|
| a directory / `file://…` | The NFS/Lustre/GPFS mount shared by login and compute nodes. |
| `s3://bucket/prefix` | MinIO or any S3-compatible store through pyarrow's native filesystem (no s3fs). Endpoint `EXAMLOPS_DIST_CHECKPOINT_S3_ENDPOINT`, else `MLFLOW_S3_ENDPOINT_URL`; credentials `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` or pyarrow's default chain — never anonymous. Needs `examlops[dataplane-files]`. |
| `gs://` / `gcs://` / `hdfs://` | Through fsspec, when it is installed. Any other scheme (http, ftp, sftp, …) is refused. |

From the dashboard's CLI console, `--checkpoint-store` accepts an `s3://` URL or a directory
inside the console workspace; an absolute path or `..` is refused there.

The layout mirrors the run directory (`<store>/<run-id>/ckpt/step-XXXXXXXX/…`), shards first and
the manifest **last**, so a step exists in the store only once complete. A checkpoint is mirrored
only if it verifies locally; a restored copy is re-verified (every shard's SHA-256 against the
manifest) before it may exist in the run directory, and a corrupt or uncommitted copy is skipped in
favour of the next older one. A store that is configured but unusable is an error (exit 2), never a
silent fallback to scratch. A failed upload is audited (`checkpoint_mirror_failed`) and retried
after the next attempt; the checkpoint keeps its scratch URI until it is actually durable. A store
that cannot be *listed* before an attempt (unreachable, access denied) is audited as
`checkpoint_store_unreadable` rather than read as empty; the attempt then runs from the local
checkpoint, if any.

Shards hold **whole tensors** whatever the strategy, so a checkpoint written under FSDP resumes
under DDP (and at a different world size) — the config hash deliberately excludes the strategy.

## Run it for real, on this machine

```bash
exa pipeline distributed run --local --nproc 2 --steps 12 --checkpoint-every 4 --max-attempts 3
exa pipeline distributed run --local --strategy fsdp
# attempt 1: success (exit 0, 3.0s)
# ✓ dist-ref-… complete: 12 steps, final loss 9.1, no resume (…/distributed/dist-ref-…)
```

This runs the reference script shipped in the package (`examlops/distributed/train_ddp.py`) under
real `torchrun`: DDP (`--strategy ddp`, the local default) or FSDP2 `fully_shard`
(`--strategy fsdp`, ZeRO-3-style parameter/optimizer sharding) on a tiny MLP with a synthetic
dataset, `nccl` when CUDA is present and `gloo` otherwise, seeded from `--seed` / `EXAMLOPS_SEED`.
It needs PyTorch (a base dependency of the workspace; without it the command exits 2).
`--checkpoint-store` works here too.

**Sharded checkpoints.** Every `--checkpoint-every` steps each rank writes its own shard
(`ckpt/step-00000004/shard-<rank>-of-<world>.pt`); rank 0 then commits `manifest.json` with each
shard's SHA-256, the step, the world size and a hash of the training config. Files are written to a
temporary name and renamed, so a killed process leaves no half-written checkpoint.

**Resume from the newest valid checkpoint.** On start the script verifies every shard hash of the
newest checkpoint; a missing, truncated or bit-flipped shard, a missing manifest or a different
training config makes *that* checkpoint invalid and it falls back to the one before. `exa` reports
a resume **only** from the run's own `EXAMLOPS_DIST_METRICS` line, cross-checked against a valid
manifest for the step — never because "this was attempt 2".

**Resubmit.** Exit codes: `0` done, `75` recoverable, `70` fatal. On a recoverable failure the
local supervisor resubmits up to `--max-attempts` with exponential backoff (`--backoff`, capped at
30 s). `--elastic-restarts N` passes `--max-restarts N` to `torchrun`.

**Fault injection.** `EXAMLOPS_DIST_FAULT_STEP=5` makes rank `EXAMLOPS_DIST_FAULT_RANK` (default 1)
`SIGKILL` itself once at the start of step 5. The run resumes from step 4 and finishes with weights
**bit-identical** to an uninterrupted run — under DDP and under FSDP, locally and through the mock
scheduler.

## What has and has not been observed

Verified here with 2 `gloo` CPU processes on one host (`tests/unit/test_distributed_torchrun.py`,
`tests/unit/test_distributed_fsdp_scheduler_torchrun.py`): DDP and FSDP training, SIGKILL →
resubmit → bit-identical resume, the generated job script submitted to the **mock** scheduler
adapter and resubmitted through it, a durable NFS-directory store, and a fresh run directory
restored from it. The Slurm and Flux multi-node / elastic scripts are rendered, syntax-checked and
their rendezvous line executed against a synthetic `SLURM_JOB_NODELIST`, but **no multi-node, NCCL,
GPU, Slurm or Flux execution has been observed**. Not built: DeepSpeed/Megatron training itself
(they run only through a model's entrypoint, and neither is a dependency), E3 GPU fractions for
distributed jobs, and an automated test of torchrun's in-job `--max-restarts` recovery (5 of 6
manual trials; a gloo reconnect race lost the sixth).

## Bookkeeping commands

```bash
exa pipeline distributed launch JPCP --nodes 2 --gpus-per-node 4      # record a planned run
exa pipeline distributed checkpoint <run-id> --step 1000 --epoch 5    # record a checkpoint
exa pipeline distributed resume <run-id>       # last integrity-valid recorded checkpoint (exit 1 if none)
exa pipeline distributed status <run-id>
exa pipeline distributed list JPCP
```

## Related

- **Phase 23** scheduler abstraction — allocates the nodes the rendezvous is read from.
- **ADR 0109** suspend/preemption — the resubmission gate.
- **B7** fine-tuning — large adapter training can use this path through an entrypoint.
- **A1/A2** — the run pins a dataset revision and emits lineage to its checkpoint.
- **D4** audit / **FinOps** — every step audited; cost recorded for `exa models cost`.
