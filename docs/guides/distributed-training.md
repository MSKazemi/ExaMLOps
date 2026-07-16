# Distributed & Fault-Tolerant Training (E6)

> Next-Gen 40 · feature **E6** · ADR 0032 · spec `design/vision/specs/E6-distributed-fault-tolerant-training.md`

E6 runs training across **multiple GPUs and nodes** (FSDP / DeepSpeed ZeRO / Megatron)
through the phase-23 scheduler abstraction, and — critically for long HPC jobs that get
preempted — **checkpoints periodically and auto-resumes** from the last valid checkpoint
instead of restarting from scratch.

Everything is pure Python and testable: launching (mock/localhost), checkpoint integrity,
corrupt-checkpoint refusal, and resume all work with no GPU, torch, or scheduler present.
In production the same launch spec emits a real `torchrun` command and the same checkpoint
records point at durable MinIO/NFS storage.

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
