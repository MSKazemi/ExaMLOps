# Inference Gateway & KV-Cache-Aware Routing (E4)

> Next-Gen 40 · feature **E4** · ADR 0039 · spec `design/vision/specs/E4-inference-gateway-kv-routing.md`

For LLM serving, *where* a request lands matters: if a replica already holds the KV/prefix
cache for a request's shared prefix (system prompt + session + RAG context), routing the
request there skips recomputing that prefix — a large latency and cost win. E4 is an
inference gateway that routes by **KV/prefix-cache affinity**, falls back to **load-aware**
routing, and keeps plain **round-robin** as the opt-out default.

The routing decision is a **pure function** of the request prefix + replica state, so the
hit-rate advantage, load fallback, SLO avoidance, and round-robin default are all testable
with no engine, GPU, or Kubernetes. In production the same logic drives a Gateway API
Inference Extension / llm-d router over real KV-event or prefix-hash indices.

## Routing policies

| Policy | When | Behaviour |
|---|---|---|
| **round-robin** | default (R3) | rotate across healthy replicas |
| **cache-aware** | opt-in (R1) | route to a replica holding the prefix (affinity); else load-aware |
| **load-aware** | fallback (R2) | least-loaded by queue depth + KV utilization + latency |

```python
from examlops.inference_gateway import InferenceGateway, Replica, prefix_key, MODE_CACHE_AWARE

gw = InferenceGateway(
    [Replica("r0"), Replica("r1"), Replica("r2")],
    mode=MODE_CACHE_AWARE,
    slo_latency_ms=500,     # avoid replicas over the latency SLO
)
key = prefix_key(system_prompt="You are…", session_id="sess-42", rag_context="doc-hash")
decision = gw.route(key)   # .replica, .decision (affinity|load_aware|round_robin), .hit
```

Serving a request warms that replica's prefix cache, so repeated shared-prefix requests hit
**affinity** — measurably beating round-robin:

```python
from examlops.inference_gateway import measure_hit_rate
measure_hit_rate(gw, [key] * 100)   # ~0.99 cache-aware vs ~0.25 round-robin on 4 replicas
```

## SLO-aware & health-aware

Replicas breaching `slo_latency_ms` are excluded from routing (R6 / GWT-5), as are unhealthy
replicas — but the gateway **never routes to nothing**: if every replica breaches, it falls
back to the healthy set rather than dropping the request.

## Prefill/decode disaggregation (optional)

Where the engine supports it, prefill and decode can run in independently-scaled pools with
KV transfer. The split is a **scheduling** concern, not a semantic one — output is identical
to the co-located path (R4 / GWT-4):

```python
from examlops.inference_gateway import disaggregated_route
disaggregated_route(key, prefill_pool=[Replica("p0")], decode_pool=[Replica("d0")])
# {"prefill_replica": "p0", "decode_replica": "d0", "identical_output": True}
```

## CLI

```bash
# Opt a model into cache-aware routing with a latency SLO:
exa serve routing set JPCP --mode cache_aware --slo-latency-ms 500

# Enable prefill/decode disaggregation:
exa serve routing set JPCP --disaggregate --prefill-pool prefill --decode-pool decode

# Simulate a shared-prefix stream — cache-aware vs round-robin hit rate:
exa serve routing simulate JPCP --replicas 4 --shared-prefix-requests 100
#   cache-aware hit rate: 99.0%
#   round-robin hit rate: 25.0%

exa serve routing stats JPCP
```

## Related

- **B2** API/policy gateway — sits in front of this inference gateway.
- **E2** engines / **E1** K8s — the replicas this routes across.
- **E5** autoscaling — routing consumes the same load signals.
- **C6** SLOs / **C1** telemetry — latency SLOs gate routing; KV/latency metrics feed it.
