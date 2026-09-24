# Suspend and resume

`examlops.suspend` is the seam behind "pause this workload, release its compute, bring it back"
(ADR 0109). It is a contract with an honest capability report, not a promise of GPU snapshots.

## What ships

| Piece | What it does |
|---|---|
| `SuspendBackend` | `snapshot` / `restore` / `discard` / `capability`, registered under the `suspend_backend` provider domain |
| `checkpoint-only` (default) | Pins the newest LangGraph checkpoint of an agent session in the agent state store (`AGENT_DB`, SQLite) and later verifies and reads it back. Application granularity; no process, container or GPU image |
| `training-checkpoint` | Pins the newest **training** checkpoint of a distributed run that verifies — ADR 0032's sharded files plus a manifest carrying each shard's SHA-256 (`examlops.distributed.checkpoint_files`). Same application granularity |
| `mock` | In-memory double for tests |
| `estimate_resume_cost` | Pure function: state size and capability in, seconds and a `basis` out |
| `preemption_promise` | Whether a broker may promise checkpoint-preserving preemption, and if not, why |
| `suspend_snapshots` table | One durable, audited row per suspended subject (`examlops.data.suspend`) |

Nothing calls the seam by default; it changes no existing behaviour.

### The training-checkpoint backend

`snapshot` asks `checkpoint_files.find_latest_valid` for the newest step whose manifest digest,
`config_hash` and every shard's SHA-256 verify — so a corrupt or half-written step is skipped and
the previous good one is pinned, and the skipped steps travel in the handle's `pointer["skipped"]`.
Nothing here re-implements hash checking.

A pin is a marker file (`.suspend-pin-<snapshot_id>.json`) written beside the manifest, not a copy
of the shards: `training.pinned_steps(run_dir)` tells a retention pass which steps a live suspend
record still needs. `discard` removes the marker and never deletes a checkpoint.

`restore` re-verifies the pinned step (which reads and hashes every shard byte, so
`state_transfer_s` is measured, not declared), refuses a step that was corrupted or rewritten since
the snapshot, and reports where a relaunch resumes from — including a note when a newer valid
checkpoint has overtaken the pin, because the training script selects the newest. It does **not**
load tensors: the job does that under torch on the training nodes.

It therefore persists what the script wrote (parameters and optimizer state) and not GPU memory,
the process image, the collective communicator, the dataloader iterator, or RNG state the run does
not re-derive from its seed. `communicator_rebuild_applicable` is `True` and
`communicator_rebuild_s` stays `None` — a relaunched run does rebuild its process group, and
nothing here has measured that.

```python
from examlops.suspend import STATE_TRAINING_RUN, service

handle = service.suspend(
    "dist-run-7",
    subject_kind=STATE_TRAINING_RUN,
    backend="training-checkpoint",
    options={"run_dir": "/state/distributed/dist-run-7"},   # default: the launcher's run dir
)
service.resume(handle.snapshot_id)
```

### The one consumer (ADR 0109 decision 3)

`EXAMLOPS_SUSPEND_PREEMPTION_GATE` (off by default) makes
`examlops.distributed.launch.supervise` consult the seam before it spends another submission on a
recoverable failure. It asks two questions — does the backend keep state in a tier that outlives
the compute (`preemption_promise`), and does *this* run have a checkpoint that verifies — and when
either answers no it declines the resubmission, records the reasons in
`SupervisedRun.preemption` and audits `distributed_resubmit_declined`, rather than restarting from
step 0 and calling it a resume. With the switch off the supervisor is unchanged, and the gate can
only ever stop a resubmission, never start one.

## What does not ship

There is **no** CRIU, `cuda-checkpoint`, vLLM sleep/wake, peer-memory or GPU-state backend. Asking
for one is refused, not emulated:

```text
SuspendUnsupported: no suspend backend 'criu' is registered ... ship one as an
'exa.providers.suspend_backend' plugin.
```

The Postgres agent checkpointer is also not covered: the checkpoint-only backend reads the SQLite
file read-only and refuses when `AGENT_DB` is unset or missing.

## Honesty rules

* A capability value nobody has stated is `None` with `basis: unknown`. `basis` is `measured`
  (folded in from recorded restores), `declared`, or `unknown`.
* `communicator_rebuild_s` is `None` when the backend has a communicator and nobody measured it.
  It is exactly `0` only for backends where none exists (application checkpoints) - a fact of the
  mechanism, flagged by `communicator_rebuild_applicable=False`.
* `estimate_resume_cost` returns `total_s=None` rather than guess. A known total carries the weakest
  basis among its parts.
* `restore` reports the timing split `state_transfer_s` versus `communicator_rebuild_s`. For the
  checkpoint-only backend `state_transfer_s` is the measured time to verify and read the pinned
  checkpoint bytes; LangGraph performs the real state load on the next invoke for that thread.
* `discard` releases the suspend record and (for `training-checkpoint`) its pin marker only. The
  checkpoint itself belongs to the runtime that wrote it and is never deleted.

## Use from Python

```python
from examlops.suspend import service, estimate_resume_cost

handle = service.suspend("thread-42", actor="alice")   # audited: suspend_snapshot
report = service.resume(handle.snapshot_id)            # audited: suspend_resume
cap = service.capability()                             # measured once restores are recorded
print(estimate_resume_cost(cap, handle.state_bytes))
```

Inspect the domain with `exa providers list --domain suspend_backend`. Select a backend with
`EXAMLOPS_SUSPEND_BACKEND_PROVIDER` or a plugin under the `exa.providers.suspend_backend`
entry-point group.
