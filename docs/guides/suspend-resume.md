# Suspend and resume

`examlops.suspend` is the seam behind "pause this workload, release its compute, bring it back"
(ADR 0109). It is a contract with an honest capability report, not a promise of GPU snapshots.

## What ships

| Piece | What it does |
|---|---|
| `SuspendBackend` | `snapshot` / `restore` / `discard` / `capability`, registered under the `suspend_backend` provider domain |
| `checkpoint-only` (default) | Pins the newest LangGraph checkpoint of an agent session in the agent state store (`AGENT_DB`, SQLite) and later verifies and reads it back. Application granularity; no process, container or GPU image |
| `training-checkpoint` | Pins the newest **training** checkpoint of a distributed run that verifies — ADR 0032's sharded files plus a manifest carrying each shard's SHA-256 (`examlops.distributed.checkpoint_files`). Same application granularity |
| `tiered-training-checkpoint` | `training-checkpoint` plus a verified, shard-differential **node-local replica** (decision 8): restores from the local tier when it still verifies, falls back to the persistent pin otherwise |
| `vllm-sleep` | Engine-delegated (decision 5): puts a running vLLM server to sleep (level 1 — weights to host RAM, KV cache dropped) and wakes it |
| `mock` | In-memory double for tests |
| `estimate_resume_cost` | Pure function: state size and capability in, seconds and a `basis` out |
| `preemption_promise` | Whether a broker may promise checkpoint-preserving preemption, and if not, why |
| `suspend_snapshots` table | One durable, audited row per suspended subject (`examlops.data.suspend`) |
| `exa pipeline distributed suspend …` | `backends` · `capability` · `snapshot` · `resume` · `discard` · `show` · `list` |
| `exa status` | A *Suspend/Resume Seam* table (and a `suspend` key under `--json`): every backend's granularity, tiers, preemption verdict and the median restore timing split |

Nothing calls the seam by default; it changes no existing behaviour.

## From the command line

```bash
exa pipeline distributed suspend backends                       # capability · tiers · preemption · restores
exa pipeline distributed suspend capability tiered-training-checkpoint
exa pipeline distributed suspend snapshot dist-run-7 --backend tiered-training-checkpoint \
    --run-dir /state/distributed/dist-run-7 --tenant research
exa pipeline distributed suspend resume <snapshot-id>             # records state_transfer_s / communicator_rebuild_s
exa pipeline distributed suspend discard <snapshot-id>            # releases the pin; never deletes the checkpoint
exa pipeline distributed suspend list --tenant research --status suspended
exa status                                                        # the seam's current ceiling, always visible
```

`snapshot`, `resume` and `discard` are audited (`suspend_snapshot`, `suspend_resume`,
`suspend_resume_failed`, `suspend_refused`, `suspend_discard`) and exit 1 on refusal with the
backend's own reason. `list --tenant` filters in SQL before the row limit. From the dashboard's CLI
console, the three mutating commands need the admin role.

### The tiered backend (ADR 0109 decision 8)

One tier cannot serve both failure classes: a node-local copy recovers a localized fault quickly and
dies with the node, the shared persistent checkpoint survives a cluster-wide outage. Set
`EXAMLOPS_SUSPEND_LOCAL_DIR` to a node-local directory and `tiered-training-checkpoint` keeps a
**verified replica** of every pinned step there:

* The replica is itself a valid ADR 0032 run directory (content-addressed `objects/` plus
  `ckpt/step-*/` hard links and the manifest), so `checkpoint_files.verify_checkpoint_dir` judges
  it — nothing re-implements hashing. Every copied object is re-hashed after the copy, and the
  replica is verified before the snapshot reports it staged.
* Staging is **differential at shard granularity**: a shard whose SHA-256 is already held is
  linked, not copied, and the pointer reports `copied_bytes` and `reused_bytes`. That is coarser
  than in-memory tensor diffs — a DDP rank's shard changes every step, so full fine-tuning reuses
  little; frozen or rarely-updated shards are what it saves.
* The tier label is **measured**: a root on `tmpfs`/`ramfs` reports `local_memory`, anything else
  `local_storage`. There is no `peer_memory` tier.
* Bounded by `EXAMLOPS_SUSPEND_LOCAL_MAX_BYTES` (default 4 GiB). A staging failure (no tier, a
  malformed cap, cap exceeded, a copy failing or a shard changing mid-copy) never fails the
  snapshot: the persistent pin stands, the half-staged replica step is removed and its orphaned
  objects collected, and `pointer.local_tier.reason` says why the replica was skipped.
* `restore` serves from the replica when it still verifies against the pinned manifest digest —
  including when the persistent copy has since been damaged — and otherwise falls back to the
  persistent tier, naming the reason in the report's `detail`.
* `discard` drops the pin, removes the replica step once no pin remains and garbage-collects
  objects no remaining replica manifest references.
* Staging, release and garbage collection take an exclusive `flock` on `<root>/.lock`, so a discard
  on one process can never collect an object another process is still copying, and two stagings
  cannot both pass the size cap and together exceed it. Restores read without the lock.

### The vLLM sleep backend (ADR 0109 decision 5)

Start the server with `--enable-sleep-mode` and `VLLM_SERVER_DEV_MODE=1`, then:

```bash
exa pipeline distributed suspend snapshot llm-a --kind serving \
    --backend vllm-sleep --base-url http://gpu-03:8000
```

`snapshot` checks `GET /is_sleeping`, calls `POST /sleep?level=1` and confirms the engine is asleep;
`restore` calls `POST /wake_up`, confirms it is awake and that `/health` answers, and records the
wake time as `state_transfer_s` (`communicator_rebuild_s` is exactly `0`: the engine's processes and
process groups survive sleep). Refused rather than emulated: a server without sleep mode (404), an
engine that is already asleep, level 2 (weights discarded — waking needs a reload this backend does
not drive), a non-`http(s)` address, and a request for GPU state (the KV cache is dropped). Its only
tier is the node's host memory, so `preemption_promise` declines on it: it serves scale-to-zero and
power damping, not preemption. The address comes from `--base-url` or `EXAMLOPS_SUSPEND_VLLM_URL`;
`EXAMLOPS_VLLM_API_KEY` is sent as a bearer token **only to the origin (scheme, host, port) of
`EXAMLOPS_SUSPEND_VLLM_URL`** — a `--base-url` naming any other host gets no key, so a caller cannot
have the platform hand its key to a server of their choosing (a keyed server elsewhere answers 401,
and the error says why). Every call is bounded by `EXAMLOPS_SUSPEND_VLLM_TIMEOUT` (default 60 s).

An engine that cannot be reached on `resume` (a transport error, a timeout or a 5xx) says nothing
about whether it is still asleep, so it is **not** recorded as a failed restore: the command exits 1,
the record stays `suspended` (audited as `suspend_resume_error`) and the resume can be retried. Only
a definite answer — the engine is not asleep, or it will not wake — marks the snapshot `failed`.

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

There is **no** CRIU, `cuda-checkpoint`, Ray actor checkpointing, peer-memory or GPU-state backend.
Asking for one is refused, not emulated:

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
