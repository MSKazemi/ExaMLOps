# Distributed & Fault-Tolerant Training (E6)

> Next-Gen 40 · feature **E6** · ADR 0032 · spec `design/vision/specs/E6-distributed-fault-tolerant-training.md`

E6 runs training across **multiple GPUs and nodes** (FSDP / DeepSpeed ZeRO / Megatron)
through the phase-23 scheduler abstraction, and — critically for long HPC jobs that get
preempted — **checkpoints periodically and auto-resumes** from the last valid checkpoint
instead of restarting from scratch.

Everything is pure Python and testable: launching (mock/localhost), checkpoint integrity,
corrupt-checkpoint refusal, and resume all work with no GPU, torch, or scheduler present.
`exa pipeline distributed launch` *plans* a run: the `torchrun` command it prints names a
placeholder `train.py`, and its checkpoints are records in `platform.db`. To actually train, use
[`exa pipeline distributed run --local`](#run-it-for-real-on-this-machine) below.

## Launch

```bash
exa pipeline distributed launch JPCP --nodes 2 --gpus-per-node 4 --strategy fsdp \
    --dataset-revision <A1-rev> --checkpoint-every 10min
# Launched dist-JPCP-2x4-fsdp — 2×4 GPUs, fsdp, rdzv node07:29500
#   torchrun --nnodes=2 --nproc_per_node=4 --rdzv_backend=c10d --rdzv_endpoint=node07:29500 …
```

The rendezvous endpoint is derived from the scheduler's allocated node list (rank-0 host);
with no scheduler node list it falls back to `localhost` for dev/CI. Strategy is selectable
per run: `fsdp` (default), `zero` (DeepSpeed ZeRO), `megatron`.

## Checkpoints are integrity-hashed

Each checkpoint records step, epoch, shard count, a durable URI, and a **SHA-256 integrity
hash** over the training state (optimizer state included), linked to the MLflow run:

```bash
exa pipeline distributed checkpoint dist-JPCP-2x4-fsdp --step 1000 --epoch 5 --shards 8 \
    --state '{"optimizer": "adam", "lr": 0.0003}'
# Checkpoint step 1000 epoch 5 → minio://checkpoints/dist-JPCP-2x4-fsdp/step-1000 (hash 7ab3…, 8 shard(s))
```

## Resume — not restart

On failure/preemption the job is resubmitted and resumes from the **last integrity-valid**
checkpoint, preserving step/epoch and optimizer state:

```bash
exa pipeline distributed resume dist-JPCP-2x4-fsdp
# Resuming dist-JPCP-2x4-fsdp from step 1000 epoch 5 (minio://…/step-1000) — optimizer state preserved.
```

### Corrupt checkpoints are refused (integrity, GWT-5)

`resume` recomputes each checkpoint's hash and **skips a corrupt one**, falling back to the
previous valid checkpoint. If none are valid it exits 1 rather than silently restarting from
scratch:

```
newest checkpoint hash mismatch → skipped
→ resume from the last valid checkpoint instead
```

## Run it for real, on this machine

```bash
exa pipeline distributed run --local --nproc 2 --steps 12 --checkpoint-every 4 --max-attempts 3
# attempt 1: success (exit 0, 3.0s)
# ✓ dist-ref-… complete: 12 steps, final loss 9.1, no resume (…/distributed/dist-ref-…)
```

This runs the reference script shipped in the package (`examlops/distributed/train_ddp.py`) under
real `torchrun`: DistributedDataParallel on a tiny MLP with a synthetic dataset, `nccl` when CUDA is
present and `gloo` otherwise, seeded from `--seed` / `EXAMLOPS_SEED`. It needs PyTorch (a base
dependency of the workspace; without it the command exits 2 with "torch is not installed"). It is a
reference job, not a registry model: it proves the mechanism, and is the template a model's
training script follows.

**Sharded checkpoints.** Every `--checkpoint-every` steps each rank writes its own shard
(`ckpt/step-00000004/shard-<rank>-of-<world>.pt`); rank 0 then commits `manifest.json` with each
shard's SHA-256, the step, the world size and a hash of the training config. Files are written to a
temporary name and renamed, so a killed process leaves no half-written checkpoint. The run
directory is `$EXAMLOPS_DATA_DIR/distributed/<run-id>` (else
`$XDG_DATA_HOME/examlops/distributed/<run-id>`).

**Resume from the newest valid checkpoint.** On start the script verifies every shard hash of the
newest checkpoint; a missing, truncated or bit-flipped shard, a missing manifest or a different
training config makes *that* checkpoint invalid and it falls back to the one before. The final line
`EXAMLOPS_DIST_METRICS=<json>` (and every manifest written afterwards) records
`resumed_from_step` and the skipped checkpoints. `exa` reports a resume **only** from that
evidence, cross-checked against a valid manifest for the step — never because "this was attempt 2".

**Resubmit.** Exit codes: `0` done, `75` recoverable (a rank was killed or preempted), `70` fatal
(bad config, non-finite loss — a resubmission would repeat it, so none is made). `torchrun` reports
any worker failure as exit 1, so the supervisor also reads the `FATAL.json` marker the script
writes. On a recoverable failure it resubmits, up to `--max-attempts`, with exponential backoff
(`--backoff`, capped at 30 s); each attempt is audited (`distributed_attempt`), the run is recorded
in `distributed_runs`, and each valid checkpoint once in `training_checkpoints`.
`--elastic-restarts N` additionally passes `--max-restarts N` to `torchrun` (in-job restart on the
same node).

**Fault injection.** `EXAMLOPS_DIST_FAULT_STEP=5` makes rank `EXAMLOPS_DIST_FAULT_RANK` (default 1)
`SIGKILL` itself once at the start of step 5. With 2 processes, the supervisor resubmits, the run
resumes from step 4 and finishes with weights **bit-identical** to an uninterrupted run (batches
depend only on seed/step/rank, and the shards restore the model and momentum exactly). This is
covered by `tests/unit/test_distributed_torchrun.py`.

**What this does and does not show.** It was verified with 2 `gloo` processes on one CPU host.
That exercises the collectives API and all the checkpoint/resume/supervisor code; it is *not*
evidence about NCCL, GPUs, multiple nodes, an interconnect, or a scheduler. Not built: Slurm/Flux
submission of this job, multi-node elastic (min/max nodes), FSDP / DeepSpeed ZeRO, shards on
MinIO/NFS, E3 GPU fractions, NCCL tuning. The in-job `--elastic-restarts` recovered in 5 of 6
local trials (a gloo reconnect race in the restarted workers lost the sixth) and has no automated test.

## Elasticity, cost, audit

- **Elasticity (R6):** where the site supports torch elastic, node loss is absorbed;
  otherwise the checkpoint-and-resubmit path above applies. `mark_failed` records a
  preemption; `resume` bumps the run's resume count.
- **Cost (R7):** `complete_run(..., cost_gpu_hours=…)` records per-run GPU-hours, feeding
  `exa models cost` and Green-AI carbon accounting.
- **Audit (R8/D4):** launch, failure, resume, corrupt-skip, and completion are all written
  to the tamper-evident audit trail.

## Inspect

```bash
exa pipeline distributed status dist-JPCP-2x4-fsdp
exa pipeline distributed list JPCP
```

## Related

- **Phase 23** scheduler abstraction — allocates the nodes/GPUs the rendezvous is built from.
- **E3** fractional GPUs — small jobs can request sub-GPU fractions.
- **B7** fine-tuning — large adapter training runs on this distributed path.
- **A1/A2** — checkpoints link to the dataset revision + lineage.
- **D4** audit / **FinOps** — failures/resumes audited; cost recorded.
