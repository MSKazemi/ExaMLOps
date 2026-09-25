"""One endpoint-picking policy interface, three implementations (ADR 0143 decisions 2, 3 and 6).

``EndpointPicker.score(request_meta, endpoints) -> ranked endpoints`` is the contract every
generative substrate satisfies:

========================  ==============================  =========================================
Substrate                 Implementation                  What runs the policy in production
========================  ==============================  =========================================
``kserve``                :class:`LLMDEndpointPicker`     llm-d's EPP behind LLMISVC
                                                          ``router.scheduler`` (GAIE InferencePool
                                                          v1); this class renders that block
``compose`` / ``hpc``     :class:`RayServeLLMPicker`      Ray Serve LLM's router (>= 2.58,
with Ray                                                  ``PrefixCacheAffinityRouter``); this
                                                          class renders its deployment config
``hpc`` plain ``vllm``    :class:`GatewayScorer`          **this module**: prefix affinity +
jobs, ``external`` pools                                  ``vllm:num_requests_waiting`` +
                                                          ``vllm:kv_cache_usage_perc`` scraped
                                                          from each replica's ``/metrics``
========================  ==============================  =========================================

The platform writes routing code only where no standard reaches — plain pools. For the two
standard-owned substrates the ``score`` method here is a *conformance model* of the runtime's
documented policy, used by the shared fixture (:func:`conformance_report`) that the ADR requires so
the three implementations stay behaviourally aligned; it never pretends to be the runtime.

Decision 3 — **tenant cache isolation and session affinity are request metadata**. Every prefix key
is salted with the project's cache salt (:func:`cache_salt_for`), so one project's prompt can never
produce a prefix hit on a replica warmed by another; the session key drives affinity. A
``kv_retention_hint`` is carried and deliberately ignored until an engine exposes a retention API.

Decision 2's **break-even gate**: the KV-aware scorer is *not* the default. Below
``EXAMLOPS_KV_ROUTING_BREAK_EVEN`` replicas (default 4), or unless ``EXAMLOPS_KV_ROUTING`` opts in,
the gateway scorer runs round-robin **with session affinity**.

Decision 6's backstop — when every endpoint is at its queued-request cap the picker raises
:class:`PoolOverloaded` carrying ``retry_after_s``; the caller maps it to ``429`` + ``Retry-After``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "SUBSTRATES",
    "EndpointPicker",
    "EndpointState",
    "GatewayScorer",
    "LLMDEndpointPicker",
    "PoolOverloaded",
    "RayServeLLMPicker",
    "RequestMeta",
    "cache_salt_for",
    "conformance_report",
    "kv_routing_enabled",
    "picker_for_substrate",
    "salted_prefix_key",
    "scrape_endpoint_state",
    "synthetic_multiturn_trace",
]

SUBSTRATES = ("kserve", "compose-ray", "hpc-ray", "hpc", "external")

#: Bounded: a request's metadata travels on every call, so its free-text fields are capped.
_MAX_KEY_LEN = 256
_MAX_WARM_KEYS = 4096  # salted prefix keys remembered per endpoint
_DEFAULT_BREAK_EVEN = 4
_DEFAULT_RETRY_AFTER_S = 2.0
_TRUTHY = {"1", "true", "yes", "on"}


class PoolOverloaded(RuntimeError):
    """Every endpoint is at its queued-request cap. Map to ``429`` + ``Retry-After``."""

    def __init__(self, retry_after_s: float, detail: str = "") -> None:
        self.retry_after_s = max(0.0, float(retry_after_s))
        self.status_code = 429
        super().__init__(detail or f"all endpoints saturated; retry after {self.retry_after_s:g}s")

    def headers(self) -> dict[str, str]:
        """The HTTP headers the gateway returns with the 429 (whole seconds, rounded up)."""
        secs = int(self.retry_after_s) + (0 if self.retry_after_s.is_integer() else 1)
        return {"Retry-After": str(max(1, secs))}


def cache_salt_for(project: str, *, key: str | None = None) -> str:
    """The per-project prefix-cache salt (decision 3).

    Keyed HMAC when ``EXAMLOPS_CACHE_SALT_KEY`` (or ``key``) is set, so a salt cannot be derived
    by a tenant who knows another project's name; a plain SHA-256 domain-separated digest
    otherwise. Either way it is stable, so a project's own cache keeps hitting.
    """
    project = (project or "default").strip() or "default"
    secret = key if key is not None else os.getenv("EXAMLOPS_CACHE_SALT_KEY", "")
    msg = f"examlops-cache-salt:{project}".encode()
    if secret:
        return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hashlib.sha256(msg).hexdigest()[:32]


def salted_prefix_key(prefix: str, cache_salt: str) -> str:
    """Prefix hash scoped to a salt: identical prompts under two salts never share a key."""
    return hashlib.sha256(f"{cache_salt}\x1f{prefix}".encode()).hexdigest()[:24]


@dataclass(frozen=True)
class RequestMeta:
    """What a generative request carries into the picker (decision 2's ``request_meta``)."""

    model: str
    prefix_hash: str
    cache_salt: str
    session_key: str | None = None
    slo_class: str = "interactive"
    tenant: str = "default"
    #: Reserved for an engine retention API (vLLM RFC #37003); ignored by every picker today.
    kv_retention_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("request_meta.model is required")
        if not self.cache_salt:
            # Fail closed: an unsalted key is exactly the cross-tenant hit decision 3 forbids.
            raise ValueError("request_meta.cache_salt is required (see cache_salt_for)")
        for name in ("prefix_hash", "cache_salt", "session_key", "slo_class", "tenant"):
            value = getattr(self, name)
            if value is not None and len(str(value)) > _MAX_KEY_LEN:
                raise ValueError(f"request_meta.{name} exceeds {_MAX_KEY_LEN} characters")

    @property
    def cache_key(self) -> str:
        return salted_prefix_key(self.prefix_hash, self.cache_salt)

    @classmethod
    def for_prompt(
        cls,
        model: str,
        prefix: str,
        *,
        project: str,
        session_key: str | None = None,
        slo_class: str = "interactive",
        tenant: str = "default",
    ) -> RequestMeta:
        return cls(
            model=model,
            prefix_hash=hashlib.sha256(prefix.encode()).hexdigest()[:24],
            cache_salt=cache_salt_for(project),
            session_key=session_key,
            slo_class=slo_class,
            tenant=tenant,
        )


@dataclass
class EndpointState:
    """One replica as the picker sees it (decision 2's ``EndpointState``)."""

    id: str
    queue_depth: int = 0  # vllm:num_requests_waiting
    running: int = 0  # vllm:num_requests_running
    kv_cache_usage: float = 0.0  # vllm:kv_cache_usage_perc, 0..1
    ttft_p99_ms: float | None = None
    network_cost: float | None = None  # optional (ADR 0117 d1b); None = unobservable
    healthy: bool = True
    #: Salted prefix keys this replica is believed to hold (updated as the picker routes).
    cached_keys: set[str] = field(default_factory=set)
    max_queued: int | None = None  # the engine's queued-request cap, when known

    @property
    def saturated(self) -> bool:
        return self.max_queued is not None and self.queue_depth >= self.max_queued


@runtime_checkable
class EndpointPicker(Protocol):
    name: str
    substrate: str
    uses_network_cost: bool

    def score(self, meta: RequestMeta, endpoints: Sequence[EndpointState]) -> list[EndpointState]:
        """Endpoints ranked best-first. Raises :class:`PoolOverloaded` when none can accept."""
        ...

    def observe(self, meta: RequestMeta, chosen: EndpointState) -> None:
        """Record that ``chosen`` served ``meta`` (warms its cache / pins the session)."""
        ...


def kv_routing_enabled(replicas: int) -> bool:
    """Decision 2's measured break-even gate for the KV-aware gateway scorer."""
    if os.getenv("EXAMLOPS_KV_ROUTING", "").strip().lower() not in _TRUTHY:
        return False
    try:
        floor = int(os.getenv("EXAMLOPS_KV_ROUTING_BREAK_EVEN", str(_DEFAULT_BREAK_EVEN)))
    except ValueError:
        floor = _DEFAULT_BREAK_EVEN
    return replicas >= max(1, floor)


def _retry_after(endpoints: Iterable[EndpointState]) -> float:
    ttfts = [e.ttft_p99_ms for e in endpoints if e.ttft_p99_ms]
    return max(ttfts) / 1000.0 if ttfts else _DEFAULT_RETRY_AFTER_S


def _admissible(endpoints: Sequence[EndpointState]) -> list[EndpointState]:
    if not endpoints:
        raise ValueError("no endpoints to pick from")
    healthy = [e for e in endpoints if e.healthy]
    if not healthy:
        raise PoolOverloaded(_DEFAULT_RETRY_AFTER_S, "no healthy endpoint")
    open_ = [e for e in healthy if not e.saturated]
    if not open_:
        raise PoolOverloaded(_retry_after(healthy))
    return open_


class _AffinityBase:
    """Shared affinity bookkeeping: session pins and salted-key warm sets."""

    name = "base"
    substrate = "external"
    uses_network_cost = False

    def __init__(self, *, session_cap: int = 10_000) -> None:
        self._sessions: dict[str, str] = {}
        self._session_cap = session_cap

    def _pinned(self, meta: RequestMeta, eps: list[EndpointState]) -> EndpointState | None:
        if not meta.session_key:
            return None
        target = self._sessions.get(f"{meta.cache_salt}:{meta.session_key}")
        return next((e for e in eps if e.id == target), None)

    def observe(self, meta: RequestMeta, chosen: EndpointState) -> None:
        key_ = meta.cache_key
        if key_ not in chosen.cached_keys:
            # Bounded like the session table: a long-lived endpoint sees unbounded distinct
            # prefixes, and a replica's real KV cache is finite anyway — forgetting a key only
            # costs an affinity hint, never correctness.
            while len(chosen.cached_keys) >= _MAX_WARM_KEYS:
                chosen.cached_keys.pop()
            chosen.cached_keys.add(key_)
        if meta.session_key:
            key = f"{meta.cache_salt}:{meta.session_key}"
            if key not in self._sessions and len(self._sessions) >= self._session_cap:
                self._sessions.pop(next(iter(self._sessions)))  # bounded: evict the oldest
            self._sessions[key] = chosen.id

    @staticmethod
    def _load(e: EndpointState, net: bool) -> float:
        cost = e.queue_depth + 0.5 * e.running + 10.0 * e.kv_cache_usage
        if net and e.network_cost is not None:
            cost += e.network_cost
        return cost

    def _rank_kv(self, meta: RequestMeta, eps: list[EndpointState]) -> list[EndpointState]:
        pinned = self._pinned(meta, eps)
        net = self.uses_network_cost

        def key(e: EndpointState) -> tuple[int, int, float, str]:
            return (
                0 if e is pinned else 1,
                0 if meta.cache_key in e.cached_keys else 1,
                self._load(e, net),
                e.id,
            )

        return sorted(eps, key=key)


class GatewayScorer(_AffinityBase):
    """The platform's own picker for plain ``vllm serve`` pools and external endpoints.

    ``kv_aware=None`` resolves the break-even gate per call (:func:`kv_routing_enabled`); below it
    the scorer is round-robin with session affinity, which is what the ADR keeps at small scale.
    """

    name = "gateway-scorer"
    substrate = "hpc"
    uses_network_cost = True

    def __init__(self, *, kv_aware: bool | None = None, session_cap: int = 10_000) -> None:
        super().__init__(session_cap=session_cap)
        self.kv_aware = kv_aware
        self._rr = count()

    def mode(self, replicas: int) -> str:
        on = self.kv_aware if self.kv_aware is not None else kv_routing_enabled(replicas)
        return "kv_aware" if on else "round_robin_affinity"

    def score(self, meta: RequestMeta, endpoints: Sequence[EndpointState]) -> list[EndpointState]:
        eps = _admissible(endpoints)
        if self.mode(len(endpoints)) == "kv_aware":
            return self._rank_kv(meta, eps)
        pinned = self._pinned(meta, eps)
        eps = sorted(eps, key=lambda e: e.id)
        start = next(self._rr) % len(eps)
        rotated = eps[start:] + eps[:start]
        if pinned is not None:
            rotated.remove(pinned)
            rotated.insert(0, pinned)
        return rotated


class RayServeLLMPicker(_AffinityBase):
    """Ray Serve LLM's router on Compose/HPC-with-Ray (decision 2, row 2).

    Production routing is Ray's; :meth:`deployment_config` renders the router selection for the
    serve config. :meth:`score` models the documented policy (prefix-cache affinity, then least
    queue) for the conformance fixture. Ray cannot observe a network-cost term, and says so.
    """

    name = "ray-serve-llm"
    substrate = "compose-ray"
    uses_network_cost = False
    MIN_RAY = "2.58.0"

    def score(self, meta: RequestMeta, endpoints: Sequence[EndpointState]) -> list[EndpointState]:
        return self._rank_kv(meta, _admissible(endpoints))

    @staticmethod
    def deployment_config(model: str, *, imbalanced_threshold: int = 10) -> dict[str, Any]:
        return {
            "llm_config": {"model_loading_config": {"model_id": model}},
            "deployment_config": {
                "request_router_config": {
                    "request_router_class": (
                        "ray.serve.llm.request_router:PrefixCacheAffinityRouter"
                    ),
                    "request_router_kwargs": {"imbalanced_threshold": int(imbalanced_threshold)},
                }
            },
            "requires": {"ray": f">={RayServeLLMPicker.MIN_RAY}"},
        }


class LLMDEndpointPicker(_AffinityBase):
    """llm-d's Endpoint Picker behind KServe LLMISVC (decision 2, row 1).

    :meth:`router_block` renders ``spec.router`` for an ``LLMInferenceService``: the standard
    ``InferencePool`` (GAIE v1) with the llm-d scheduler — and **no** ``InferenceObjective`` /
    ``InferenceModel`` (moved out of GAIE in v1.6.0). The EPP can use a network-cost scorer.
    """

    name = "llm-d-epp"
    substrate = "kserve"
    uses_network_cost = True

    def score(self, meta: RequestMeta, endpoints: Sequence[EndpointState]) -> list[EndpointState]:
        return self._rank_kv(meta, _admissible(endpoints))

    @staticmethod
    def router_block() -> dict[str, Any]:
        return {
            "router": {
                "gateway": {},
                "route": {},
                "scheduler": {
                    "pool": {"apiVersion": "inference.networking.k8s.io/v1"},
                    "scorers": ["prefix-cache", "load", "lora-affinity"],
                },
            }
        }


def picker_for_substrate(substrate: str, *, kv_aware: bool | None = None) -> EndpointPicker:
    """The one picker implementation decision 2 assigns to ``substrate``."""
    s = (substrate or "").strip().lower()
    if s == "kserve":
        return LLMDEndpointPicker()
    if s in ("compose-ray", "hpc-ray"):
        picker = RayServeLLMPicker()
        picker.substrate = s
        return picker
    if s in ("hpc", "external", "compose"):
        scorer = GatewayScorer(kv_aware=kv_aware)
        scorer.substrate = s
        return scorer
    raise ValueError(f"unknown substrate {substrate!r}; expected one of {SUBSTRATES}")


# ── /metrics scraping for the gateway scorer ─────────────────────────────────


def scrape_endpoint_state(
    endpoint_id: str,
    base_url: str,
    *,
    fetch: Callable[[str], str] | None = None,
    max_queued: int | None = None,
    timeout_s: float = 2.0,
) -> EndpointState:
    """Build an :class:`EndpointState` from a vLLM server's ``/metrics``.

    A scrape that fails marks the endpoint unhealthy rather than guessing its load — a replica
    that cannot answer ``/metrics`` should not be the best-ranked one.
    """
    from examlops.engines.vllm_server import parse_prometheus_text

    getter = fetch or _http_get(timeout_s)
    try:
        text = getter(base_url.rstrip("/") + "/metrics")
    except Exception:  # noqa: BLE001 - an unreachable replica is unhealthy, not an error
        return EndpointState(id=endpoint_id, healthy=False, max_queued=max_queued)
    m = parse_prometheus_text(text)
    usage = m.get("vllm:kv_cache_usage_perc", m.get("vllm:gpu_cache_usage_perc", 0.0))
    return EndpointState(
        id=endpoint_id,
        queue_depth=int(m.get("vllm:num_requests_waiting", 0.0)),
        running=int(m.get("vllm:num_requests_running", 0.0)),
        kv_cache_usage=min(1.0, max(0.0, float(usage))),
        max_queued=max_queued,
    )


def _http_get(timeout_s: float) -> Callable[[str], str]:
    def get(url: str) -> str:
        import urllib.request

        if not url.startswith(("http://", "https://")):
            raise ValueError("metrics URL must be http(s)")
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
            return resp.read(1_000_000).decode("utf-8", "replace")  # bounded read

    return get


# ── the shared conformance fixture (ADR 0143 Verification 1) ─────────────────


def synthetic_multiturn_trace(
    *, projects: int = 3, sessions_per_project: int = 4, turns: int = 5, shared_prompt: bool = True
) -> list[RequestMeta]:
    """A deterministic multi-turn trace. With ``shared_prompt`` every project sends the *same*
    system prompt, which is exactly the case where an unsalted cache would leak hits."""
    trace: list[RequestMeta] = []
    for t in range(turns):
        for p in range(projects):
            for s in range(sessions_per_project):
                prefix = "SYSTEM: be helpful" if shared_prompt else f"SYSTEM p{p}"
                trace.append(
                    RequestMeta.for_prompt(
                        "chat",
                        f"{prefix}|session-{p}-{s}|turn-{t}" if not shared_prompt else prefix,
                        project=f"project-{p}",
                        session_key=f"s-{p}-{s}",
                    )
                )
    return trace


def conformance_report(
    picker: EndpointPicker, trace: Sequence[RequestMeta], replicas: int = 4
) -> dict[str, Any]:
    """Replay ``trace`` through ``picker``; report session affinity and cross-salt hits.

    ``affinity_rate`` = share of non-first turns of a session routed to the replica that served its
    previous turn. ``cross_salt_hits`` counts requests routed to a replica *only* because another
    salt warmed the same unsalted prefix — which must be 0 for every implementation.
    """
    endpoints = [EndpointState(id=f"r{i}") for i in range(replicas)]
    last: dict[str, str] = {}
    owner: dict[tuple[str, str], str] = {}  # (replica, cached key) -> salt that warmed it
    same = total = cross = 0
    for meta in trace:
        ranked = picker.score(meta, endpoints)
        chosen = ranked[0]
        sess = f"{meta.cache_salt}:{meta.session_key}" if meta.session_key else None
        if sess is not None and sess in last:
            total += 1
            same += int(last[sess] == chosen.id)
        # A cross-salt hit: the replica already holds this request's identity (salted key, or -
        # for a picker that forgot to salt - the raw prefix hash) and another salt warmed it.
        for ident in (meta.cache_key, meta.prefix_hash):
            warmer = owner.get((chosen.id, ident))
            if warmer is not None and warmer != meta.cache_salt:
                cross += 1
                break
        before = set(chosen.cached_keys)
        picker.observe(meta, chosen)
        for added in chosen.cached_keys - before:
            owner[(chosen.id, added)] = meta.cache_salt
        if sess is not None:
            last[sess] = chosen.id
    return {
        "picker": picker.name,
        "requests": len(trace),
        "affinity_rate": (same / total) if total else None,
        "cross_salt_hits": cross,
        "distinct_cache_keys": len({m.cache_key for m in trace}),
        "distinct_prefixes": len({m.prefix_hash for m in trace}),
    }
