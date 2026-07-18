"""Next-Gen 40 · E4 — inference gateway & KV-cache-aware routing (ADR 0039).

Routes LLM requests to the engine replica most likely to already hold their KV/prefix cache
(**affinity**), falling back to **load-aware** routing when there is no affinity, and to
plain **round-robin** by default (cache-aware routing is opt-in). SLO-breaching replicas are
avoided. Optional prefill/decode **disaggregation** is modelled as an opt-in per model.

The routing decision is a **pure function** of the request prefix + replica state, so the
prefix-cache hit rate, load fallback, SLO avoidance, and round-robin default are all
testable with no engine, GPU, or K8s. In production the same logic drives a Gateway API
Inference Extension / llm-d router over real KV-event/prefix-hash indices.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from itertools import count

from examlops import data as platform_db

MODE_ROUND_ROBIN = "round_robin"
MODE_CACHE_AWARE = "cache_aware"


@dataclass
class Replica:
    id: str
    queue_depth: int = 0
    kv_utilization: float = 0.0  # 0..1
    latency_ms: float = 0.0
    healthy: bool = True
    # Approximate prefix-hash index: the set of prefix keys this replica currently holds.
    prefixes: set[str] = field(default_factory=set)

    def load_score(self) -> float:
        """Lower is better: queue + KV pressure + normalized latency."""
        return self.queue_depth + self.kv_utilization * 10.0 + self.latency_ms / 100.0


@dataclass
class RouteDecision:
    replica: Replica
    decision: str  # affinity | load_aware | round_robin
    hit: bool
    prefix_key: str


def prefix_key(system_prompt: str = "", session_id: str = "", rag_context: str = "") -> str:
    """Stable key for the shared prefix (system prompt + session + RAG context) (R1)."""
    payload = f"{system_prompt}‖{session_id}‖{rag_context}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class InferenceGateway:
    """A KV-cache-aware inference router over a fixed replica set.

    ``mode`` selects the policy; ``slo_latency_ms`` (if set) excludes replicas breaching the
    latency SLO from routing (R6/GWT-5). The gateway records prefix→replica affinity as it
    routes, so repeated shared-prefix requests hit the same replica (R1/GWT-1).
    """

    def __init__(
        self,
        replicas: list[Replica],
        *,
        model: str = "-",
        tenant: str = "default",
        mode: str = MODE_ROUND_ROBIN,
        slo_latency_ms: float | None = None,
        persist: bool = False,
    ) -> None:
        if not replicas:
            raise ValueError("at least one replica is required")
        self.replicas = replicas
        self.model = model
        self.tenant = tenant
        self.mode = mode
        self.slo_latency_ms = slo_latency_ms
        self.persist = persist
        self._rr = count()

    def _eligible(self) -> list[Replica]:
        elig = [r for r in self.replicas if r.healthy]
        if self.slo_latency_ms is not None:
            within = [r for r in elig if r.latency_ms <= self.slo_latency_ms]
            # Never route to nothing: if every replica breaches SLO, fall back to all healthy.
            elig = within or elig
        return elig or self.replicas

    def route(self, key: str) -> RouteDecision:
        """Route a request identified by its ``prefix_key`` (R1/R2/R3/GWT-1..3/GWT-5)."""
        eligible = self._eligible()

        if self.mode == MODE_CACHE_AWARE:
            holders = [r for r in eligible if key in r.prefixes]
            if holders:
                # Affinity: pick the least-loaded replica that already holds the prefix.
                replica = min(holders, key=Replica.load_score)
                decision = RouteDecision(replica, "affinity", hit=True, prefix_key=key)
            else:
                # No affinity → load-aware fallback (R2/GWT-2).
                replica = min(eligible, key=Replica.load_score)
                decision = RouteDecision(replica, "load_aware", hit=False, prefix_key=key)
        else:
            # Round-robin default (R3/GWT-3).
            replica = eligible[next(self._rr) % len(eligible)]
            decision = RouteDecision(
                replica, "round_robin", hit=key in replica.prefixes, prefix_key=key
            )

        # Serving the request warms this replica's prefix cache for next time.
        decision.replica.prefixes.add(key)
        if self.persist:
            platform_db.record_routing_event(
                self.model,
                key,
                decision.replica.id,
                decision.decision,
                decision.hit,
                tenant=self.tenant,
            )
        return decision


def disaggregated_route(
    key: str,
    prefill_pool: list[Replica],
    decode_pool: list[Replica],
) -> dict:
    """Split a request across a prefill pool and a decode pool (R4/GWT-4).

    Prefill routes cache-aware; decode routes load-aware. Output is identical to the
    co-located path (the split is a scheduling concern, not a semantic one), which this
    models by returning both endpoints for the same request.
    """
    if not prefill_pool or not decode_pool:
        raise ValueError("both prefill and decode pools are required for disaggregation")
    prefill_gw = InferenceGateway(prefill_pool, mode=MODE_CACHE_AWARE)
    decode = min(decode_pool, key=Replica.load_score)
    prefill = prefill_gw.route(key)
    return {
        "prefix_key": key,
        "prefill_replica": prefill.replica.id,
        "decode_replica": decode.id,
        "identical_output": True,  # split is scheduling-only (R4)
    }


def measure_hit_rate(gateway: InferenceGateway, keys: list[str]) -> float:
    """Replay a request stream and return the observed prefix-cache hit rate (GWT-1)."""
    hits = 0
    for k in keys:
        if gateway.route(k).hit:
            hits += 1
    return hits / len(keys) if keys else 0.0


__all__ = [
    "MODE_ROUND_ROBIN",
    "MODE_CACHE_AWARE",
    "Replica",
    "RouteDecision",
    "InferenceGateway",
    "prefix_key",
    "disaggregated_route",
    "measure_hit_rate",
]
