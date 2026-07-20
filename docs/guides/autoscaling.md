# Autoscaling & Scale-to-Zero (E5)

> Next-Gen 40 · feature **E5** · ADR 0031 · spec `design/vision/specs/E5-autoscaling.md`

E5 gives every served model a **metric-driven autoscaler** with scale-to-zero, managed
cold starts, an optional warm pool, and anti-thrash controls. The scaling decision is a
**pure function** — current replicas + observed metric + policy + clock → decision — so it
is fully testable and the *same* logic can drive a KEDA/Knative generator or an in-process
controller. No cluster is required.

Scale events are audited (D4); measured cold-start times surface to C6 SLOs; scale-to-zero
windows feed FinOps savings.

## The policy

| Field | Default | Meaning |
|---|---|---|
| `min_replicas` | 1 | Floor (set `0` to allow scale-to-zero) |
| `max_replicas` | 4 | Ceiling |
| `target_metric` | `queue_depth` | `rps` / `queue_depth` / `gpu_util` / `p95` |
| `target_value` | 10 | Desired value of the metric per replica |
| `scale_to_zero_after_s` | 0 | Idle seconds before scaling to zero (`0` disables) |
| `warm_pool` | 0 | Replicas always kept warm (avoids cold start) |
| `stabilization_s` | 30 | No change within this window since the last scale |
| `cooldown_s` | 60 | No scale-*down* within this window |
| `gpu_fraction` | 1.0 | E3 GPU fraction per replica (savings accounting) |

```bash
exa serve autoscale set JPCP --min 0 --max 8 --metric queue_depth --target 10 \
    --scale-to-zero-after 300 --warm-pool 0 --gpu-fraction 0.5
```

## How the decision is made

`decide_scale` computes the replica count that would bring the metric to `target_value`
(`ceil(observed / target)`), clamped to `[min|0, max]`, then applies the guards:

- **Scale-to-zero** — when `idle_seconds ≥ scale_to_zero_after_s`, target the warm pool
  (0 if none). Deferred if inside the cooldown.
- **Stabilization** — any change is blocked if the last scale was more recent than
  `stabilization_s` (prevents flapping on noisy metrics).
- **Cooldown** — scale-*down* is blocked inside `cooldown_s` (scale-*up* is not, so load
  spikes are served immediately).

Simulate a decision without touching a cluster:

```bash
exa serve autoscale simulate JPCP --replicas 2 --observed 45
# JPCP: 2 → 5 replicas — queue_depth=45 → scale up to 5

exa serve autoscale simulate JPCP --replicas 2 --observed 45 --since-last 5
# JPCP: 2 = 2 replicas — within stabilization window
#   held by anti-thrash: stabilization

exa serve autoscale simulate JPCP --replicas 1 --observed 0 --idle 400
# JPCP: 1 → 0 replicas — idle 400s ≥ 300s → scale to 0
```

`--json` returns `{desired_replicas, current_replicas, changed, reason, blocked_by}` for a
controller to act on.

## Recording executed scales

A controller (or a test) records what it actually did; the event is audited and, if a cold
start was measured, feeds the SLO layer:

```bash
exa serve autoscale record JPCP 0 1 --reason cold --cold-start 3.2
exa serve autoscale status JPCP
```

`status` shows the policy, the recent scale events, and the **mean measured cold-start**
(`not measured` until at least one is recorded).

## Savings

Scale-to-zero avoids running replicas while idle. The estimate counts scale-to-zero
transitions × the idle window × the GPU fraction × cost:

```bash
exa serve autoscale savings JPCP --gpu-cost 2.0
# JPCP: 2 scale-to-zero event(s) → 1.0 GPU-hours saved ($2.0).
```

## Graceful degradation

Everything here is pure Python over the shared `platform.db` — no Kubernetes, KEDA, or GPU
is required to declare policies, simulate decisions, or account savings. In a live cluster
the same `decide_scale` output drives the replica controller; offline it still answers
"what *would* it do?" for planning and CI.

## Related

- **E3** GPU sharing & fractional allocation — `gpu_fraction` feeds savings accounting.
- **C6** SLOs & error budgets — cold-start times inform latency SLOs.
- **D4** tamper-evident audit — every executed scale is a hash-chained audit event.
- **FinOps / Green-AI** — scale-to-zero savings roll into cost/carbon accounting.
