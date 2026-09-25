# Admission seam and quota reservations

ExaMLOps splits "may this job run, and where" from "run it and tell me when it is done"
(ADR 0116). The execution side is the existing `SchedulerAdapter` (Slurm, Flux, mock) and is
untouched. The admission side is `examlops.admission_seam`: a typed request, a pluggable policy,
and two-phase quota reservations.

**Off by default.** `exa admission submit/stats`, the queue table and the control plane's admission
accounting are byte-identical. Work dispatches through the seam only when
`EXAMLOPS_ADMISSION_DISPATCH_ENABLED` is truthy; unset, the two callers below open no datastore,
decide nothing and write nothing.

| Caller | What it does when the switch is on |
|---|---|
| `exa pipeline run` | Decides before the run starts, holds the run's quota for the run's duration, releases it however the run ends |
| `examlops.scheduler_jobs.submit` (every platform job on mock/Slurm/Flux: asset builds, embedding reindexes) | Decides before `submit_job`; binds the reservation to the scheduler job id and commits it in one statement; the job's terminal state (`update_hpc_job`) releases it, including on failure. A refused admission submits nothing; a scheduler that rejects the job gets the quota back at once |
| `exa serve llm start --launcher slurm\|flux` (`HpcLauncher`) | Admits the serving allocation (GPUs per node × nodes, `workload_class: serving`); a server never reaches a terminal state, so `stop` releases its quota after cancelling the job |

`tests/unit/test_admission_seam_submit_paths.py` lists every `submit_job` call in the product trees
and fails on a new one, so a path to the scheduler that skips admission cannot be added quietly.

## What ships

| Piece | What it does |
|---|---|
| `JobRequest` | Frozen, versioned (`schema_version: 1`), JSON-serializable. GPUs/CPUs/memory/nodes, `gang`, `network_tier` (`scale_up`/`scale_out`/`wan_tolerant`), `scale_up_domain`, `queue`, `priority_class`, `deadline`, `flexibility_s`, project/tenant. Unknown fields and impossible combinations are refused with every reason listed |
| `AdmissionPolicy.decide` | `decide(request, cluster_state, quotas)` returns `Admit`, `Queue` or `Reject`, each with a reason |
| `fair-share` (default) | The existing fair-share logic (global cap, per-tenant cap, max-min fairness) as pure functions. A test proves it picks exactly what `claim_next_admission` picks |
| `baseline-over-quota` | Per-tenant GPU baseline (guaranteed), over-quota borrowing of idle GPUs, hard limit. Select with `EXAMLOPS_ADMISSION_POLICY` |
| Capability probe | `probe(adapter)` returns `supports_gang` / `supports_preempt` / `supports_reservations`, each `True`, `False` or `None` (unknown). Existing adapters are unchanged and report unknown; the mock reports `False` |
| `preempt(adapter, job_id)` | Refuses with `PreemptUnsupported` unless the backend declared it can. It never kills and restarts to imitate preemption |
| `quota_reservations` | `reserved` to `committed` to `released`, or `expired` after a TTL. Held reservations count against the project's GPU limit and GPU-hour headroom |
| `render_kueue` | Pure generator of ResourceFlavor / AdmissionCheck / ClusterQueue / LocalQueue manifests. Not applied |

## Commands (read-only)

```bash
exa admission simulate --request job.json
exa admission simulate --request job.json --policy baseline-over-quota --cluster-state whatif.json
exa admission reservations --state reserved
exa admission reservations --expire-preview
exa admission reconcile --dry-run
exa admission topology --json
exa admission translate --request job.json --backend flux
```

`simulate` takes no lock and writes no audit event and no reservation. It lists the request
fields the chosen policy did not look at (`ignored_fields`), so `fair-share` visibly ignores
`gang` rather than appearing to honour it.

```json
{"project": "research", "tenant": "team-a",
 "resources": {"gpus": 8, "nodes": 2}, "gang": true,
 "network_tier": "scale_up", "scale_up_domain": "required", "est_runtime_s": 7200}
```

## Per-tenant GPU quotas

`baseline-over-quota` reads `EXAMLOPS_ADMISSION_QUOTAS`, a JSON file. A malformed file is an
error; quotas are never silently ignored.

```json
{"tenants": {"team-a": {"baseline_gpus": 4, "limit_gpus": 8, "over_quota_weight": 2}}}
```

## Two-phase reservation

A reservation holds GPUs and GPU-hours against a project at admission. The GPU limit is the
project's `gpu_limit` (a stored `0` means no quota was declared). GPU-hour headroom is the budget
minus recorded spend in the budget's period, minus what is already held. The check and the insert
run under one scoped write lock, so two concurrent reservers cannot both take the last slot.

A `reserved` row whose TTL (`EXAMLOPS_RESERVATION_TTL_S`, default 900) has lapsed holds nothing
immediately. The next reserve or sweep marks it `expired`, so a leak is visible in
`exa admission reservations`. Every transition is audited (`quota_reserved`,
`quota_reservation_refused`, `quota_committed`, `quota_released`, `quota_reservation_expired`),
as is every decision taken by a non-default policy (`admission_decision`).

A `committed` reservation bound to a scheduler job (`hpc:<scheduler>:<job id>`) is never
TTL-swept, because a running job outlives any admission TTL. It is released when the job's terminal
state is recorded, when `exa serve llm stop` stops a server, or by `exa admission reconcile`, which
asks the scheduler for each such job and releases those that have ended. Several paths never record
a terminal state (a server that hits its wall time, a Slurm/Flux reindex that succeeds, an asset
build whose wait timed out), so run `exa admission reconcile` periodically. A job the scheduler
cannot answer for, or reports `UNKNOWN`, keeps its quota.

GPUs are counted as the backend will allocate them: Slurm's `gpus` is the job total, Flux's is
per slot (one slot per node unless `ntasks` is set), `gpus_per_node` is multiplied by the node
count, a node range counts its upper bound, and a typed value (`a100:2`) is a count. A GPU value
that is not a count is refused rather than admitted as zero GPUs.

## External gates behind `decide()`

`EXAMLOPS_ADMISSION_GATES` names the gates consulted after the policy would admit
(`policy,budget,carbon`). Unset, no gate runs. Each gate answers `allow`, `deny` (the request is
rejected), `defer` (queued) or `abstain` (nothing to say). Deny overrides defer; every gate's
answer is listed under `gates` in `exa admission simulate --json`, and a decision a gate changed is
audited as `admission_gate_blocked`.

| Gate | Denies / defers when | Abstains when |
|---|---|---|
| `policy` | A `policy.yaml` rule for `action: admission` says `deny` (or `require_approval`, which defers). `when:` sees `project`, `tenant`, `workload_class`, `gpus`, `cpus`, `memory_gb`, `nodes`, `gang`, `network_tier`, `scale_up_domain`, `priority_class`, `queue`, `flexibility_s`, `gpu_hours` | Never; no matching rule is the engine's documented `allow` |
| `budget` | The project's budget period is breached, or the request's GPU-hours would breach it | The project has no budget |
| `carbon` | A **decision-type** (marginal) grid signal is above `EXAMLOPS_ADMISSION_CARBON_MAX_G` and the request is flexible (`flexibility_s > 0`) with room before its deadline: deferred | No threshold set, or the feed is an average/accounting signal (ADR 0112: shifting on it can raise total emissions) |

A gate that cannot answer (the budget store raises, the policy engine breaks, the threshold does
not parse, an unknown gate name is configured) **denies** with `unverified: …`. Sites add their own
gate (an evaluation or burn-in check) with `examlops.admission_seam.gates.register_gate`.

```yaml
# policy.yaml
policies:
  - action: admission
    when: "gpus > 16 and priority_class != 'critical'"
    effect: require_approval
```

## Resource graph and scale-up domains

`examlops.admission_seam.topology` builds a typed graph: `cluster`, `node`, `accelerator`,
`scale_up_domain`, `fabric`, `power`, `storage` vertices joined by `contains` and `connects`
edges (only the declared pairs are allowed). Nodes and GPUs come from the `hpc_nodes` inventory
(`exa hpc nodes`). Everything the inventory cannot know is declared by the site in
`EXAMLOPS_RESOURCE_TOPOLOGY` (YAML or JSON), else `<config dir>/topology.yaml`:

```yaml
scale_up_domains:
  nvl72-a: {nodes: [gpu01, gpu02]}
  nvl72-b: {nodes: [cluster2/gpu01]}   # <cluster>/<node> when a name is ambiguous
power:
  pdu-1: {cap_kw: 40, nodes: [gpu01, gpu02]}
fabrics:
  ib0: {kind: infiniband, nodes: [gpu01, gpu02]}
storage:
  lustre: {nodes: [gpu01]}
```

The admission state's `largest_free_domain_gpus` is the free GPUs in the fullest-free declared
domain. A node's GPUs count as free only when the node is `idle` (`mixed` counts as zero) and its
inventory row is younger than `EXAMLOPS_RESOURCE_TOPOLOGY_MAX_AGE_S` (default 3600; `0` disables
the check), so an `idle` read long ago is not evidence of room now. With no
file, or a file that cannot be read, the topology is **unknown** and `scale_up_domain: required`
is refused a promise; with a file, a request no single domain can hold is **queued**, never placed
spanning domains. Nodes the file names but the inventory lacks, and a node in two domains, are
listed as problems by `exa admission topology`. Where the backend is Flux, Fluxion's own graph
places the job; this graph only answers the admission question.

## One request, every backend

`examlops.admission_seam.translate` maps a `JobRequest` onto an adapter's `resources` dict (the
*native* half: nodes, GPUs, CPUs per task, memory on Slurm, queue, a wall-time limit rounded up
from `est_runtime_s`) plus a canonical JSON *envelope* for everything no scheduler enforces (gang,
network tier, scale-up domain, priority class, flexibility, deadline, project, tenant).
`from_native` rebuilds the request from the envelope and fails with `TranslationMismatch` if the
native half disagrees. The tests submit one fixture through the real mock, Slurm and Flux adapters
(with a recording executor), parse the argv they emit, and get the same request back. `not_native`
lists per backend what it cannot enforce; Flux, for instance, has no schedulable memory.

## What does not ship

- `exa retrain` (which goes through the control plane), Ray Serve / KServe / Compose serving and a
  Prefect flow started outside `exa pipeline run` do not dispatch through the seam.
- No reclamation, preemption or time-based fairness. A baseline request with no free GPU is queued
  and says reclamation is not implemented.
- No built-in evaluation-status or burn-in gate: a `JobRequest` names no model and the platform
  records no burn-in state. Both plug in through `register_gate`.
- No Kueue adapter. The rendered `AdmissionCheck` names a controller that does not exist here, and
  the manifests are unvalidated against a live Kueue. Slurm and Flux clusters, weighted
  over-quota shares, several flavors, gang and topology requests are refused, not approximated.
- `flexibility_s` and `deadline` are read by the `carbon` gate only; no fair-share policy uses them.
- The Kueue backend has no `translate` mapping and no execution adapter.
