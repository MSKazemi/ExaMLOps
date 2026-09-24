"""Built-in LLMOps calculation providers (ADR 0083 — programmable MLOps, INC-5b).

Registers **four LLMOps calculation domains** on the existing provider registry (ADR 0074/0077):

* ``llm_cost``    — token usage → USD (default: ``token-rate``).
* ``llm_cache``   — semantic-cache savings metrics (default: ``hit-savings``).
* ``llm_routing`` — per-candidate routing score (default: ``least-cost``).
* ``rag_quality`` — RAG retrieval quality metrics (default: ``retrieval-lite``).

Pre-emptive: no live B-track backend uses these yet. When B2–B4/B8 land, each calls
``get_provider("<domain>")`` and never hard-codes a formula — exactly the discipline that made
``cost``/``carbon`` swappable from their first commit (ADR 0077 §3).

Importing this module registers all four domains as a side effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .providers import Provider, ProviderMeta, register_provider

# ── llm_cost ──────────────────────────────────────────────────────────────────

_DOMAIN_COST = "llm_cost"

# OpenAI / Anthropic convention: reasoning tokens billed at the output rate.
_DEFAULT_INPUT_PRICE = 0.002  # USD per 1k tokens (illustrative; override via config)
_DEFAULT_OUTPUT_PRICE = 0.002  # USD per 1k tokens


def _f(inputs: Mapping[str, Any], key: str, default: float) -> float:
    val = inputs.get(key, default)
    return float(val if val is not None else default)


class TokenRateCostProvider(Provider):
    """Default LLM cost formula — token price card.

    ``cost_usd = prompt_tokens/1000 × input_price_per_1k
              + (completion_tokens + reasoning_tokens)/1000 × output_price_per_1k``

    Reasoning tokens are billed at the output rate (OpenAI o-series / Anthropic
    extended-thinking convention).  Prices default to illustrative values; override
    via ``input_price_per_1k`` / ``output_price_per_1k`` in the call inputs or via
    a ``providers.yaml`` formula.
    """

    name = "token-rate"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "cost_usd = prompt_tokens/1000 × input_price_per_1k "
                "+ (completion_tokens + reasoning_tokens)/1000 × output_price_per_1k. "
                "Reasoning tokens billed at the output rate (OpenAI/Anthropic convention)."
            ),
            units={"cost_usd": "USD"},
            outputs=("cost_usd",),
            params=(
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "input_price_per_1k",
                "output_price_per_1k",
            ),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        prompt = _f(inputs, "prompt_tokens", 0.0)
        completion = _f(inputs, "completion_tokens", 0.0)
        reasoning = _f(inputs, "reasoning_tokens", 0.0)
        input_price = _f(inputs, "input_price_per_1k", _DEFAULT_INPUT_PRICE)
        output_price = _f(inputs, "output_price_per_1k", _DEFAULT_OUTPUT_PRICE)
        cost = (prompt / 1000.0) * input_price + (completion + reasoning) / 1000.0 * output_price
        return {"cost_usd": round(cost, 6)}


# ── llm_cache ─────────────────────────────────────────────────────────────────

_DOMAIN_CACHE = "llm_cache"


class HitSavingsCacheProvider(Provider):
    """Default semantic-cache savings provider.

    Computes:
    * ``hit_rate`` = cache_hits / total_calls  (0 when total_calls == 0)
    * ``cost_saved_usd`` = cache_hits × cost_per_call_usd
    * ``latency_saved_ms`` = cache_hits × max(0, latency_full_ms − latency_cached_ms)
    """

    name = "hit-savings"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "hit_rate = cache_hits / total_calls; "
                "cost_saved_usd = cache_hits × cost_per_call_usd; "
                "latency_saved_ms = cache_hits × max(0, latency_full_ms − latency_cached_ms). "
                "Division guarded: total_calls == 0 → hit_rate = 0."
            ),
            units={"cost_saved_usd": "USD", "latency_saved_ms": "ms"},
            outputs=("hit_rate", "cost_saved_usd", "latency_saved_ms"),
            params=(
                "total_calls",
                "cache_hits",
                "cost_per_call_usd",
                "latency_full_ms",
                "latency_cached_ms",
            ),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        total_calls = _f(inputs, "total_calls", 0.0)
        cache_hits = _f(inputs, "cache_hits", 0.0)
        cost_per_call = _f(inputs, "cost_per_call_usd", 0.0)
        latency_full = _f(inputs, "latency_full_ms", 0.0)
        latency_cached = _f(inputs, "latency_cached_ms", 0.0)
        hit_rate = cache_hits / total_calls if total_calls > 0 else 0.0
        cost_saved = cache_hits * cost_per_call
        latency_saved = cache_hits * max(0.0, latency_full - latency_cached)
        return {
            "hit_rate": round(hit_rate, 4),
            "cost_saved_usd": round(cost_saved, 6),
            "latency_saved_ms": round(latency_saved, 2),
        }


# ── llm_routing ───────────────────────────────────────────────────────────────

_DOMAIN_ROUTING = "llm_routing"

_NEG_INF = float("-inf")


class LeastCostRoutingProvider(Provider):
    """Default routing scorer — cheapest healthy candidate wins.

    Returns ``{"score": −cost_usd}`` for a healthy candidate, ``−inf`` if unhealthy.
    The caller maximizes scores, so the cheapest reachable route is selected.

    A ``cost-latency`` variant (``−(cost_usd + latency_ms × latency_weight)``) ships as
    an expression example in providers.yaml; it demonstrates that the *formula*, not just
    the coefficients, is swappable.
    """

    name = "least-cost"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "score = −cost_usd if healthy else −inf. "
                "Caller maximizes: selects cheapest reachable route. "
                "Override with a cost-latency formula: −(cost_usd + latency_ms × weight)."
            ),
            outputs=("score",),
            params=("cost_usd", "latency_ms", "quality", "healthy"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        healthy = inputs.get("healthy", True)
        if not healthy:
            return {"score": _NEG_INF}
        cost = _f(inputs, "cost_usd", 0.0)
        return {"score": -cost}


# ── rag_quality ───────────────────────────────────────────────────────────────

_DOMAIN_RAG = "rag_quality"

_DEFAULT_RELEVANCE_THRESHOLD = 0.5


class RetrievalLiteQualityProvider(Provider):
    """Default RAG retrieval quality provider.

    Computes:
    * ``precision`` = hits / k   where hits = count(retrieved_relevances[i] ≥ threshold)
    * ``recall``    = hits / relevant_total  (guarded: relevant_total == 0 → 0)
    * ``relevance`` = mean(retrieved_relevances)  (empty list → 0)

    Ragas / MRR / NDCG variants can be registered as entry-point plugins or YAML formulas.
    """

    name = "retrieval-lite"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "precision = hits/k; recall = hits/relevant_total; "
                "relevance = mean(retrieved_relevances). "
                "hits = count(relevance_score ≥ threshold, default 0.5). "
                "Ragas / MRR / NDCG variants available as plugins or YAML formulas."
            ),
            outputs=("precision", "recall", "relevance"),
            params=("retrieved_relevances", "relevant_total", "k", "threshold"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        retrieved: list[float] = [float(r) for r in inputs.get("retrieved_relevances", [])]
        relevant_total = _f(inputs, "relevant_total", 0.0)
        k = _f(inputs, "k", float(len(retrieved)) or 1.0)
        threshold = _f(inputs, "threshold", _DEFAULT_RELEVANCE_THRESHOLD)
        hits = sum(1 for r in retrieved if r >= threshold)
        precision = hits / k if k > 0 else 0.0
        recall = hits / relevant_total if relevant_total > 0 else 0.0
        relevance = sum(retrieved) / len(retrieved) if retrieved else 0.0
        return {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "relevance": round(relevance, 4),
        }


# ── gateway façade (ADR 0107 / R-A10) ─────────────────────────────────────────


def cache_savings_via_provider(
    *,
    total_calls: int,
    cache_hits: int,
    cost_saved_usd: float,
    provider: str | None = None,
) -> dict[str, float] | None:
    """Hit-rate + cost-saved via the ``llm_cache`` provider — or ``None`` if none is selected.

    ``None`` means: the caller keeps computing ``hit_rate = hits/total`` and ``cost_saved_usd``
    directly from the already-summed ``cache_events`` rows — today's exact math, unchanged when no
    provider is selected at all. Same "default is the legacy path, opt in to swap the formula"
    rule as :func:`estimate_llm_cost_via_provider` — this is about the *unconfigured* case; an
    *explicitly* selected provider, including the built-in default, is a formal contract with its
    own declared rounding, not a promise to reproduce an unrounded float bit-for-bit.

    ``cost_saved_usd`` is already a sum over many individual calls, whereas the provider's own
    formula (``cache_hits × cost_per_call_usd``) is written for a single representative call — so a
    per-call average (``cost_saved_usd / cache_hits``) is derived here and handed in. For the
    default ``hit-savings`` provider this reconstructs the already-summed total to the 6 decimal
    places it declares (:class:`HitSavingsCacheProvider`); a custom provider is free to do
    something else with it (weight recent calls differently, apply a different distribution
    assumption, …), which is the whole point of the formula being swappable.
    """
    import os as _os

    from .providers import get_provider
    from .providers.loader import load_domain_config

    try:
        block = load_domain_config(_DOMAIN_CACHE)
    except Exception:
        block = {}
    name = provider or _os.getenv("EXAMLOPS_LLM_CACHE_PROVIDER") or block.get("provider")
    if not name:
        return None  # no explicit selection — caller keeps its own aggregate
    try:
        prov = get_provider(_DOMAIN_CACHE, name=name, config=block)
        cost_per_call = (cost_saved_usd / cache_hits) if cache_hits else 0.0
        out = prov.compute(
            {
                "total_calls": total_calls,
                "cache_hits": cache_hits,
                "cost_per_call_usd": cost_per_call,
                **{k: v for k, v in block.items() if k != "provider"},
            }
        )
        return {
            "hit_rate": float(out.get("hit_rate", 0.0)),
            "cost_saved_usd": float(out.get("cost_saved_usd", cost_saved_usd)),
        }
    except Exception:
        # A bad plugin/config must never break stats reporting — degrade to the caller's own math.
        return None


def route_score_via_provider(
    *,
    cost_usd: float,
    healthy: bool = True,
    latency_ms: float | None = None,
    quality: float | None = None,
    provider: str | None = None,
) -> float:
    """One deployment's routing score via the ``llm_routing`` provider (ADR 0153 d2, `cost_aware`).

    Unlike the cost/cache facades above, this one always returns a usable value rather than
    ``None`` on "nothing configured": the domain's registered default (``least-cost``, "cheapest
    healthy candidate wins") is safe to run with zero configuration — selecting among deployments
    is the `cost_aware` strategy's entire job, so there is no legacy caller-side formula to defer to.
    A broken plugin degrades to ``0.0`` (no preference) rather than breaking routing.
    """
    import os as _os

    from .providers import get_provider
    from .providers.loader import load_domain_config

    try:
        block = load_domain_config(_DOMAIN_ROUTING)
    except Exception:
        block = {}
    name = provider or _os.getenv("EXAMLOPS_LLM_ROUTING_PROVIDER") or block.get("provider")
    try:
        prov = get_provider(_DOMAIN_ROUTING, name=name, config=block)
        out = prov.compute(
            {
                "cost_usd": cost_usd,
                "healthy": healthy,
                "latency_ms": latency_ms if latency_ms is not None else 0.0,
                "quality": quality if quality is not None else 0.0,
                **{k: v for k, v in block.items() if k != "provider"},
            }
        )
        score = out.get("score")
        return float(score) if score is not None else _NEG_INF
    except Exception:
        return 0.0


def rag_quality_via_provider(
    *,
    retrieved_relevances: list[float],
    relevant_total: int,
    k: int,
    threshold: float = _DEFAULT_RELEVANCE_THRESHOLD,
    provider: str | None = None,
) -> dict[str, float] | None:
    """RAG retrieval quality via the ``rag_quality`` provider — or ``None`` if none is selected.

    ``None`` means: the caller keeps its own set-membership precision/recall math — today's exact
    behaviour, unchanged, following the same rule as the other facades in this module.
    """
    import os as _os

    from .providers import get_provider
    from .providers.loader import load_domain_config

    try:
        block = load_domain_config(_DOMAIN_RAG)
    except Exception:
        block = {}
    name = provider or _os.getenv("EXAMLOPS_RAG_QUALITY_PROVIDER") or block.get("provider")
    if not name:
        return None
    try:
        prov = get_provider(_DOMAIN_RAG, name=name, config=block)
        out = prov.compute(
            {
                "retrieved_relevances": retrieved_relevances,
                "relevant_total": relevant_total,
                "k": k,
                "threshold": threshold,
                **{kk: v for kk, v in block.items() if kk != "provider"},
            }
        )
        return {
            "precision": float(out.get("precision", 0.0)),
            "recall": float(out.get("recall", 0.0)),
            "relevance": float(out.get("relevance", 0.0)),
        }
    except Exception:
        return None


def estimate_llm_cost_via_provider(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    provider: str | None = None,
) -> float | None:
    """Cost for one generation via the ``llm_cost`` provider — or ``None`` if none is selected.

    Returning ``None`` when nothing is configured is deliberate. The default ``token-rate``
    provider carries *generic* price defaults, whereas C1's rate table knows that a locally
    served model (llama3.1, nomic-embed-text) costs **zero** per token — the common case on
    an HPC-sovereign platform. Silently routing every call through the generic default would
    invent a dollar cost for self-hosted inference.

    So the provider is consulted only when an operator has actually chosen one (a
    ``--provider`` override, ``EXAMLOPS_LLM_COST_PROVIDER``, or a ``provider:`` key in the
    ``[llm_cost]`` config block); otherwise the caller keeps its existing estimate. This is
    the same "default is byte-identical to the legacy path" rule the carbon and cost
    providers follow (ADR 0074/0083).
    """
    import os as _os

    from .providers import get_provider
    from .providers.loader import load_domain_config

    try:
        block = load_domain_config(_DOMAIN_COST)
    except Exception:
        block = {}
    name = provider or _os.getenv("EXAMLOPS_LLM_COST_PROVIDER") or block.get("provider")
    if not name:
        return None  # no explicit selection — caller keeps its own estimate
    try:
        prov = get_provider(_DOMAIN_COST, name=name, config=block)
        out = prov.compute(
            {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                **{k: v for k, v in block.items() if k != "provider"},
            }
        )
        cost = out.get("cost_usd")
        return float(cost) if cost is not None else None
    except Exception:
        # A bad plugin/config must never break a generation — degrade to the caller's path.
        return None


# ── registration ──────────────────────────────────────────────────────────────


def register_builtins() -> None:
    """Register all four LLMOps provider domains on the global registry (idempotent)."""
    register_provider(_DOMAIN_COST, "token-rate", TokenRateCostProvider, default=True)
    register_provider(_DOMAIN_CACHE, "hit-savings", HitSavingsCacheProvider, default=True)
    register_provider(_DOMAIN_ROUTING, "least-cost", LeastCostRoutingProvider, default=True)
    register_provider(_DOMAIN_RAG, "retrieval-lite", RetrievalLiteQualityProvider, default=True)


# Register at import time (matches the carbon/cost/placement/drift/promotion convention).
register_builtins()
