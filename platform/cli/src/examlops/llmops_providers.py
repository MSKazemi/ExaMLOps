"""Built-in LLMOps calculation providers (ADR 0083 — programmable MLOps, INC-5b).

Registers **four LLMOps calculation domains** on the existing provider registry (ADR 0074/0077):

* ``llm_cost``    — token usage → USD (default: ``token-rate``).
* ``llm_cache``   — semantic-cache savings metrics (default: ``hit-savings``).
* ``llm_routing`` — per-candidate routing score (default: ``least-cost``).
* ``rag_quality`` — RAG retrieval quality metrics (default: ``retrieval-lite``).

Every domain has a live call site through the façades below (gateway cost, cache stats,
``cost_aware`` routing, RAG quality); each resolves ``--provider`` → ``EXAMLOPS_<DOMAIN>_PROVIDER``
→ the ``<domain>:`` block of ``providers.yaml`` (legacy fallback: ``finops.yaml``) and never
hard-codes a formula (ADR 0077 §3). :func:`describe_active_providers` feeds the F10 console.

Importing this module registers all four domains as a side effect.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .providers import Provider, ProviderMeta, register_provider

logger = logging.getLogger(__name__)

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

    The built-in ``cost-latency`` variant (:class:`CostLatencyRoutingProvider`,
    ``−(cost_usd + latency_ms × latency_weight)``) demonstrates that the *formula*, not just
    the coefficients, is swappable.
    """

    name = "least-cost"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "score = −cost_usd if healthy else −inf. "
                "Caller maximizes: selects cheapest reachable route. "
                "Swap for the built-in cost-latency formula: −(cost_usd + latency_ms × weight)."
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


_DEFAULT_LATENCY_WEIGHT = 0.001  # USD-equivalent per millisecond of TTFT (override via config)


class CostLatencyRoutingProvider(Provider):
    """Routing scorer that trades cost against latency (ADR 0083, the ``cost-latency`` variant).

    ``score = −(cost_usd + latency_ms × latency_weight)`` for a healthy candidate, ``−inf`` if not.
    ``latency_weight`` converts milliseconds into the same unit as ``cost_usd`` (default
    ``0.001`` — one second of time-to-first-token weighs as much as one dollar); set it in the
    ``llm_routing`` block of ``providers.yaml``. It exists to prove the ADR's claim that the
    *formula*, not just its coefficients, is swappable: selecting it changes which term decides a
    route, with no core-code change. A negative weight is refused (it would reward slowness).
    """

    name = "cost-latency"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "score = −(cost_usd + latency_ms × latency_weight) if healthy else −inf. "
                "Caller maximizes: cheapest-and-fastest reachable route wins. "
                f"latency_weight defaults to {_DEFAULT_LATENCY_WEIGHT} (USD per ms)."
            ),
            outputs=("score",),
            params=("cost_usd", "latency_ms", "latency_weight", "healthy"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if not inputs.get("healthy", True):
            return {"score": _NEG_INF}
        weight = _f(inputs, "latency_weight", _DEFAULT_LATENCY_WEIGHT)
        if weight < 0:
            raise ValueError(f"latency_weight must be >= 0, got {weight}")
        cost = _f(inputs, "cost_usd", 0.0)
        latency = max(0.0, _f(inputs, "latency_ms", 0.0))
        return {"score": -(cost + latency * weight)}


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


# ── config resolution shared by every façade (ADR 0083 "Config home") ─────────────────────────

#: The four LLMOps domains, in the order the dashboard and ``describe_active_providers`` list them.
LLMOPS_DOMAINS: tuple[str, ...] = (_DOMAIN_COST, _DOMAIN_CACHE, _DOMAIN_ROUTING, _DOMAIN_RAG)


def domain_config(domain: str) -> dict[str, Any]:
    """The config block for one LLMOps domain.

    ADR 0083 puts these blocks in ``providers.yaml`` (one flat ``<domain>:`` block, the home
    ADR 0077 gave every non-finops domain). The first façades read ``finops.yaml``'s
    ``finops: <domain>:`` block instead, so a site that followed them keeps working: that block is
    the fallback, consulted only when ``providers.yaml`` has none. A malformed or unreadable file
    degrades to ``{}`` (the registered default) — a calculation never stops over config.
    """
    from .providers.loader import load_domain_config

    try:
        block = load_domain_config(domain, group=domain)
    except Exception:
        logger.warning("%s: providers.yaml block failed to load; ignoring it", domain)
        block = {}
    if block:
        return block
    try:
        return load_domain_config(domain)  # legacy finops.yaml home
    except Exception as exc:
        logger.warning("%s: legacy finops.yaml block failed to load; ignoring it: %s", domain, exc)
        return {}


def selected_provider_name(
    domain: str, *, override: str | None = None, block: Mapping[str, Any] | None = None
) -> str | None:
    """The operator's explicit choice: ``--provider`` → ``EXAMLOPS_<DOMAIN>_PROVIDER`` → config.

    ``None`` means nobody chose — each façade decides what that means (most keep the caller's own
    legacy math; routing uses the registered default).
    """
    import os as _os

    cfg = block if block is not None else domain_config(domain)
    name = override or _os.getenv(f"EXAMLOPS_{domain.upper()}_PROVIDER") or cfg.get("provider")
    return str(name) if name else None


#: What runs when an operator selects NO provider for an opt-in domain. The ``llm_cost``,
#: ``llm_cache`` and ``rag_quality`` façades return ``None`` on "nothing selected" and the caller
#: keeps its own arithmetic, so the registered default provider does not run there. Showing the
#: default's methodology for them would put a formula on screen that never computed the figure.
#: ``llm_routing`` is absent on purpose: its registered default always runs.
_CALLER_METHODOLOGY: dict[str, str] = {
    _DOMAIN_COST: (
        "No llm_cost provider selected: the gateway prices a call from its built-in per-model "
        "rate table (examlops.telemetry.genai.estimate_cost): prompt/1000 × rate_in + "
        "completion/1000 × rate_out. Self-hosted models in the table cost 0; an unknown model "
        "uses a conservative default rate."
    ),
    _DOMAIN_CACHE: (
        "No llm_cache provider selected: hit_rate = hits / total calls, and cost saved is the "
        "sum of the per-event cost_saved recorded in cache_events."
    ),
    _DOMAIN_RAG: (
        "No rag_quality provider selected: set-membership precision (relevant retrieved / "
        "retrieved) and recall (relevant retrieved / relevant)."
    ),
}


def describe_active_providers() -> list[dict[str, Any]]:
    """What computes each LLMOps figure, and how — for the F10 console (ADR 0083).

    One row per domain. When an operator selected a provider, or for ``llm_routing`` (whose
    registered default always runs), the row carries that provider's name, version and full
    :class:`ProviderMeta` (``mode: "provider"``). For the opt-in domains with nothing selected the
    caller's own arithmetic runs, not the registered default, so the row says exactly that
    (``mode: "builtin"``, ``provider: None``, the caller's methodology): the text on screen is the
    formula that ran. A provider that fails to resolve is reported with ``ok: False`` and its
    error — never dropped, never a crash (the ADR 0076 discoverability invariant).

    Resolution happens in the calling process: an ``EXAMLOPS_<DOMAIN>_PROVIDER`` override is that
    process's env, so a dashboard reports the gateway's choice only when both see the same config
    directory and env.
    """
    from .providers import default_provider_name, get_provider

    rows: list[dict[str, Any]] = []
    for domain in LLMOPS_DOMAINS:
        block = domain_config(domain)
        chosen = selected_provider_name(domain, block=block)
        default = default_provider_name(domain)
        name = chosen or default
        row: dict[str, Any] = {
            "domain": domain,
            "provider": name,
            "selected": chosen is not None,
            "default": default,
            "mode": "provider",
            "ok": True,
            "error": None,
        }
        if chosen is None and domain in _CALLER_METHODOLOGY:
            row.update(
                provider=None,
                mode="builtin",
                version="",
                methodology=_CALLER_METHODOLOGY[domain],
                uncertainty=None,
                units={},
                outputs=[],
                params=[],
                source="",
            )
            rows.append(row)
            continue
        try:
            prov = get_provider(domain, name=name, config=block)
            meta = prov.metadata()
            row.update(
                provider=getattr(prov, "name", name),
                version=getattr(prov, "version", ""),
                methodology=meta.methodology,
                uncertainty=meta.uncertainty,
                units=dict(meta.units),
                outputs=list(meta.outputs),
                params=list(meta.params),
                source=meta.source,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        rows.append(row)
    return rows


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
    from .providers import get_provider

    block = domain_config(_DOMAIN_CACHE)
    name = selected_provider_name(_DOMAIN_CACHE, override=provider, block=block)
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
        # A bad plugin/config must never break stats reporting — degrade to the caller's own math,
        # but visibly: a silent fallback here is indistinguishable from "no provider configured".
        logger.warning(
            "llm_cache provider %r failed; cache_stats() falls back to its own math", name
        )
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
    from .providers import get_provider

    block = domain_config(_DOMAIN_ROUTING)
    name = selected_provider_name(_DOMAIN_ROUTING, override=provider, block=block)
    try:
        prov = get_provider(_DOMAIN_ROUTING, name=name, config=block)
        out = prov.compute(
            {
                # Config coefficients first, runtime observations last: a block key named like
                # an input (``cost_usd``, ``healthy``, ``latency_ms``) must never replace what the
                # gateway measured for this deployment, or every candidate would score the same.
                **{k: v for k, v in block.items() if k != "provider"},
                "cost_usd": cost_usd,
                "healthy": healthy,
                "latency_ms": latency_ms if latency_ms is not None else 0.0,
                "quality": quality if quality is not None else 0.0,
            }
        )
        score = out.get("score")
        return float(score) if score is not None else _NEG_INF
    except Exception:
        # A silent 0.0 here is indistinguishable from a real tie between deployments — log it so
        # a routing decision that looks wrong is traceable to a broken provider, not a mystery.
        logger.warning("llm_routing provider %r failed; scoring this deployment as 0.0", name)
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
    from .providers import get_provider

    block = domain_config(_DOMAIN_RAG)
    name = selected_provider_name(_DOMAIN_RAG, override=provider, block=block)
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
        logger.warning(
            "rag_quality provider %r failed; falling back to set-membership precision/recall", name
        )
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
    from .providers import get_provider

    block = domain_config(_DOMAIN_COST)
    name = selected_provider_name(_DOMAIN_COST, override=provider, block=block)
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
        # A bad plugin/config must never break a generation — degrade to the caller's path, but
        # audibly: a silent fallback is indistinguishable from "no provider configured".
        logger.warning("llm_cost provider %r failed; the caller's own estimate is used", name)
        return None


# ── registration ──────────────────────────────────────────────────────────────


def register_builtins() -> None:
    """Register all four LLMOps provider domains on the global registry (idempotent)."""
    register_provider(_DOMAIN_COST, "token-rate", TokenRateCostProvider, default=True)
    register_provider(_DOMAIN_CACHE, "hit-savings", HitSavingsCacheProvider, default=True)
    register_provider(_DOMAIN_ROUTING, "least-cost", LeastCostRoutingProvider, default=True)
    register_provider(_DOMAIN_ROUTING, "cost-latency", CostLatencyRoutingProvider)
    register_provider(_DOMAIN_RAG, "retrieval-lite", RetrievalLiteQualityProvider, default=True)


# Register at import time (matches the carbon/cost/placement/drift/promotion convention).
register_builtins()
