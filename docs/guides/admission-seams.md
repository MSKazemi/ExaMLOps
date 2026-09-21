# Admission seam and quota reservations

ExaMLOps splits "may this job run, and where" from "run it and tell me when it is done"
(ADR 0116). The execution side is the existing `SchedulerAdapter` (Slurm, Flux, mock) and is
untouched. The admission side is `examlops.admission_seam`: a typed request, a pluggable policy,
and two-phase quota reservations.

**Nothing here changes existing behaviour.** `exa admission submit/stats`, the queue table and the
control plane's admission accounting are byte-identical. The seam is a library plus two read-only
commands; nothing dispatches through it yet.

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

## What does not ship

- No queue worker or scheduler path calls the seam, and nothing releases a reservation on job
  completion or failure. Callers must commit and release.
- No reclamation, preemption or time-based fairness. A baseline request with no free GPU is queued
  and says reclamation is not implemented.
- No `job_admission` policy hook, and no carbon, evaluation or burn-in signal in the decision.
- No typed resource graph; capacity comes from the existing node snapshots.
- No Kueue adapter. The rendered `AdmissionCheck` names a controller that does not exist here, and
  the manifests are unvalidated against a live Kueue. Slurm and Flux clusters, weighted
  over-quota shares, several flavors, gang and topology requests are refused, not approximated.
- `flexibility_s` and `deadline` are carried and echoed; no policy shifts work on them yet.
