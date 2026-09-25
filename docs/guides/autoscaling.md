# Autoscaling & Scale-to-Zero (E5)

> Next-Gen 40 · feature **E5** · ADR 0031 · spec `design/vision/specs/E5-autoscaling-scale-to-zero.md`

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

## The controller (`exa serve autoscale run`)

The controller is the loop that turns `decide_scale` into an executed change. Each cycle, for every
model with a policy: read signals -> `decide_scale` -> apply -> audit.

```bash
exa serve autoscale run --once                       # dry run (default): audits, changes nothing
EXAMLOPS_AUTOSCALE_ENABLED=1 exa serve autoscale run --apply --once
EXAMLOPS_AUTOSCALE_ENABLED=1 exa serve autoscale run --apply        # loop every 30 s
```

| Safety | Behaviour |
|---|---|
| Kill-switch | `EXAMLOPS_AUTOSCALE_ENABLED` (default off). Off: `--apply` is refused and audited. |
| Dry run | Default. No lease, no applier call, no scale event; the would-be decision is audited. |
| Lease | One controller acts at a time (`EXAMLOPS_AUTOSCALE_LEASE_TTL`). |
| Storm cap | `EXAMLOPS_AUTOSCALE_MAX_CHANGES` changes per cycle; extras are refused, audited. |
| Absent signal | Held and audited. Absent is never 0. Same for an unreachable Prometheus. |
| Below min | Only when idleness over `scale_to_zero_after_s` was measured as exactly zero traffic. |
| Applier error | Audited (`autoscale_apply_failed`), counted, retried next cycle; no scale event. |
| Anti-thrash | Stabilization/cooldown read from the recorded scale events, across cycles. |

**Signals.** Read from Prometheus (`PROMETHEUS_URL`); the controller and the KEDA generator share
one definition per metric (`examlops.autoscale.queries`), so a KEDA trigger evaluates exactly the
query the controller reads.

| Metric | Source | What it measures |
|---|---|---|
| `rps` | `sum(rate(examlops_predict_requests_total[1m]))` | requests per second |
| `p95` | `histogram_quantile(0.95, … examlops_predict_latency_seconds_bucket[5m])` | 95th-percentile latency |
| `queue_depth` | `sum(rate(examlops_predict_latency_seconds_sum[1m]))` | mean requests **in flight** (queued + executing), by Little's law `L = λ·W` — Knative's `concurrency` |
| `gpu_util` | only `EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY` | your GPU exporter's utilisation |

`queue_depth` lags a burst by its 1-minute window — the model server exports no leading per-replica
queue gauge. Override it with `EXAMLOPS_AUTOSCALE_QUEUE_DEPTH_QUERY` (e.g. the Knative queue-proxy's
`revision_app_request_concurrency` on KServe). `gpu_util` has no default because the exporter's
labels are site knowledge; set a template where `{model}` is the regex-escaped model name (backslashes
doubled, ready to sit inside a double-quoted `=~"…"` matcher, since PromQL strings use Go escapes)
and `{k8s_name}` its DNS-1123 form:

```bash
export EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY='avg(DCGM_FI_DEV_GPU_UTIL{pod=~"{k8s_name}-predictor-.*"})'
```

A template that names no model (it would scale every model on one fleet-wide number) or is longer
than 2000 characters is refused; the controller then holds the model as *signal source down*
(audited) rather than reading a value. Without a template a `gpu_util` policy holds as *signal
absent*.

**GPU-aware packing (E3).** Set `EXAMLOPS_AUTOSCALE_GPU_CAPACITY` to the GPUs the autoscaler may
commit (fractions count: a replica with `gpu_fraction: 0.25` uses 0.25). Each cycle sums
`replicas × gpu_fraction` over every policy; a scale-**up** that would exceed the capacity is refused
(`autoscale_refused`, audited) while scale-downs always proceed. A model whose replicas are unknown
counts at its `max_replicas` (capacity that cannot be accounted for is treated as taken), and an
unparsable or non-positive value is capacity **0** — a typo never lifts the ceiling. Unset =
unbounded. Dry runs pack exactly as a real cycle would.

**Appliers.** `record` writes the scale event and audit and touches no serving substrate; the change
is then made by an operator or an external system. Current replicas are the last recorded
`to_replicas`, so seed a model once with `exa serve autoscale record MODEL 1 1`; an unseeded model
holds. `desired` writes the decided count to an `autoscale_desired` row (`exa serve autoscale status`
shows it) and reads it back as the current count; that is **intent for whatever owns replicas** - it
changes no running replica. `k8s` patches the model's predictor Deployment through the Kubernetes
API `scale` subresource (see below). `ray` is **not built**: Ray Serve here scales one deployment
(every model in it) at deploy time via `RAY_AUTOSCALE_MAX_REPLICAS`, and no admin route changes one
model's replicas, so it refuses.

### The `k8s` applier

```bash
EXAMLOPS_AUTOSCALE_ENABLED=1 exa serve autoscale run --apply --applier k8s
```

It reads and writes `apps/v1` `deployments/<target>/scale` with a merge patch, where `<target>` is
`EXAMLOPS_AUTOSCALE_K8S_TARGET` with `{name}` = the DNS-1123 model name (default
`{name}-predictor`, the Deployment KServe raw-deployment mode creates) in
`EXAMLOPS_AUTOSCALE_NAMESPACE` (default: the service account's namespace, else `default`).

| Setting | Default | Notes |
|---|---|---|
| `EXAMLOPS_K8S_API` | in-cluster `https://$KUBERNETES_SERVICE_HOST:$KUBERNETES_SERVICE_PORT` | plain `http` only to a loopback address (`kubectl proxy`) |
| `EXAMLOPS_K8S_TOKEN_FILE` | service-account token | a remote API with no token is refused, never called anonymously |
| `EXAMLOPS_K8S_CA_FILE` | service-account `ca.crt` | the server certificate is verified |
| `EXAMLOPS_K8S_TIMEOUT` | 10 | seconds per request, clamped to 1–60 |

**One writer of replicas.** Before patching it lists the namespace's HorizontalPodAutoscalers (paged)
and **refuses** if one targets the Deployment — a KEDA `ScaledObject` materialises as
`keda-hpa-<name>`. Use either the generated KEDA/Knative objects or this applier for a model, not
both. If the HPA list cannot be read, the patch is not attempted (fail closed). The service account
needs `get`/`patch` on `deployments/scale`, `get` on `deployments`, and `list` on
`horizontalpodautoscalers`. A 403 on the patch is reported as missing RBAC; a misconfigured API
fails `run --apply` up front (exit 2).

Verified here against a faithful fake API server and the real urllib transport on loopback; no live
cluster is available to this repository's tests.

## Defaults in the model YAML

A model YAML may declare an `autoscale:` block (same keys as `exa serve autoscale set`):

```yaml
autoscale:
  min_replicas: 0
  max_replicas: 6
  target_metric: rps          # rps | p95 | queue_depth | gpu_util
  target_value: 5
  scale_to_zero_after_s: 300  # needs min_replicas: 0
  warm_pool: 0
```

The block is the **default**; a row set with `exa serve autoscale set` overrides it per model
(`exa serve autoscale status` prints `policy_source: yaml|db`). No shipped pack declares one, so
nothing changes until a pack opts in. Invalid blocks are reported by
`examlops.autoscale.policy_yaml.validate_autoscale_block` and ignored by the controller.

## KEDA and Knative/KServe manifests

```bash
exa serve autoscale manifest JPCP --kind keda      # KEDA ScaledObject (Prometheus trigger)
exa serve autoscale manifest JPCP --kind knative   # KServe overlay with Knative annotations
```

Generator output only: nothing is applied and no chart installs KEDA or Knative. Only `rps` and
`p95` and `queue_depth` policies always render, `gpu_util` only with
`EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY` set — otherwise it is refused rather than emitted as a trigger
that never fires. The Knative overlay supports `rps` and `queue_depth` (as Knative's `concurrency`). Assumed and unverified
without a cluster: the `scaleTargetRef` (`<model>-predictor`, override with `--target`) and the
Prometheus address KEDA queries.

## Cold starts: the activator

A request for a model the autoscaler took to zero must bring a replica back and wait for it. The
activator (`examlops.autoscale.activator`) does that:

- **Single flight.** The first cold request scales the model to `max(1, warm_pool, min_replicas)`
  and polls readiness; concurrent requests for the same model wait on it (one scale-up per burst).
  Requests arriving while the wake is in progress join it even if replicas already read >0.
- **Bounded.** At most `EXAMLOPS_AUTOSCALE_ACTIVATOR_MAX_WAITERS` (64) requests wait per model; the
  next is answered `503 overloaded` at once. Each wait is bounded by the request's own deadline; a
  request whose deadline is already spent is answered at once and triggers no scale-up.
- **Backs off after a failure.** A wake that fails (API refused, HPA owns the Deployment, GPU
  capacity) is not retried by every following request: for `EXAMLOPS_AUTOSCALE_ACTIVATOR_TTL`
  seconds the same error is answered at once, so a storm of cold requests is not a storm of API
  calls and audit rows. A timeout is not backed off — its scale-up stands.
- **GPU-aware.** With `EXAMLOPS_AUTOSCALE_GPU_CAPACITY` set, the wake is checked against the same
  fleet ceiling the controller enforces; a wake that would exceed it is refused
  (`autoscale_activation_refused`, audited) and answered `503 cold_start`.
- **Measured and audited.** Scale-up → ready is recorded as the scale event's `cold_start_s`
  (`autoscale_event`), which `exa serve autoscale status` averages as `mean_cold_start_s` for the
  cold-start SLO (C6). A timeout (`autoscale_activation_timeout`) or failed scale-up
  (`autoscale_activation_failed`) is audited too; after a timeout the scale-up stands.
- **Absent is not zero.** No policy, a policy that never reaches zero, or replicas the applier cannot
  report → the request is passed through untouched. A warm model is cached for
  `EXAMLOPS_AUTOSCALE_ACTIVATOR_TTL` (5 s) so the hot path does not ask on every request.

Readiness comes from the applier when it can count ready pods (`k8s`: `status.readyReplicas`),
otherwise from the model server's OIP route `GET {EXAMLOPS_AUTOSCALE_READY_URL}/v2/models/<m>/ready`
(default `RAY_SERVE_URL`).

**On the request path.** With `EXAMLOPS_AUTOSCALE_ACTIVATOR=1` (default off) the inference router
(`serving/inference_pipeline`, `ModelRouter.route`) holds each request in the activator before
routing it; the applier is `EXAMLOPS_AUTOSCALE_APPLIER` (default `desired`). A cold start that does
not finish in time answers `503 cold_start` with `Retry-After`; an activator bug is logged and the
request routed as before. A model the activator remembers warm is answered on the event loop with no
thread hop; cold waits run on their own bounded pool (`EXAMLOPS_AUTOSCALE_ACTIVATOR_THREADS`,
default 16), never the loop's default executor, so a burst of cold requests cannot starve warm
traffic, and the router answers by the request deadline even if a probe or API call hangs. The wake is single-flight per router process — another router replica
waking the same model concurrently issues the same (idempotent) scale-up.

By hand, or to pre-warm before a known burst:

```bash
exa serve autoscale activate JPCP --applier k8s --timeout 300
```

## Prefetch plan

`exa serve autoscale prefetch` lists which models to keep warm (`warm_pool > 0`) or pre-pull (a
zero-able model with recent traffic). Read-only planning; the node weight cache and registry
prefetcher are **not built** (warm pools are enforced by the decision itself).

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

The transitions are counted over the model's **whole** history, not a page of recent events, so
the figure keeps growing with the model rather than flattening once it has scaled more times than
one listing returns. `status` deliberately differs: the events it shows, and the mean cold-start it
reports, are about *recent* behaviour, which is what makes them useful for judging the policy.
A total and a recent sample are different questions, and this page is the total.

## Graceful degradation

Everything here is pure Python over the shared `platform.db` — no Kubernetes, KEDA, or GPU
is required to declare policies, simulate decisions, or account savings. In a live cluster
the same `decide_scale` output drives the replica controller; offline it still answers
"what *would* it do?" for planning and CI.

## Related

- **E3** GPU sharing & fractional allocation — `gpu_fraction` feeds savings accounting and the
  `EXAMLOPS_AUTOSCALE_GPU_CAPACITY` packing guard.
- **C6** SLOs & error budgets — cold-start times inform latency SLOs.
- **D4** tamper-evident audit — every executed scale is a hash-chained audit event.
- **FinOps / Green-AI** — scale-to-zero savings roll into cost/carbon accounting.
