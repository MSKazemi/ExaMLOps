"""B3 — Semantic caching for the model gateway (ADR 0018).

Returns a stored completion for an embedding-**similar**, cacheable prompt, with
per-tenant + per-params isolation, safe bypass, TTL/size eviction, and measured savings.
The production embedder is a local model (Ollama `nomic-embed` / sentence-transformers, as
in Phase 25) and the ANN store is Redis/Qdrant (B5); the **fallback** is an in-process
cosine search over a deterministic token-hash embedding, so the cache logic is exercisable
with no vector DB and no embedding service.

Wires into the B2 gateway via its existing ``cache_lookup``/``cache_store`` hooks
(:func:`bind_to_gateway`).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_EMBED_DIM = 64


# ── Embedding (pluggable; deterministic token-hash fallback) ──────────────────


def _default_embed(text: str) -> list[float]:
    """Bag-of-tokens hashing embedding — paraphrases sharing tokens land close (cosine)."""
    vec = [0.0] * _EMBED_DIM
    tokens = [t for t in text.lower().split() if t]
    for tok in tokens:
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest(), 16)
        vec[h % _EMBED_DIM] += 1.0
    return vec


def _embed(text: str) -> list[float]:
    """Try a real local embedder; fall back to the deterministic token-hash vector."""
    try:  # pragma: no cover - optional local embedding backend
        import os

        if os.getenv("EXAMLOPS_CACHE_EMBED_BACKEND"):
            from examlops.skipper.embeddings import embed_text  # type: ignore

            return list(embed_text(text))
    except Exception:
        pass
    return _default_embed(text)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── Cache ─────────────────────────────────────────────────────────────────────


@dataclass
class CacheEntry:
    embedding: list[float]
    prompt: str
    completion: Any
    namespace: str
    created_at: float
    tokens: int = 0
    cost_usd: float = 0.0


#: Params that change what a model returns, and so must change the cache namespace. A caller
#: pinning a ``seed`` for reproducibility, capping ``max_tokens``, or asking for a JSON schema is
#: asking a different question than the same prompt without them.
_KEY_PARAMS = ("temperature", "max_tokens", "top_p", "stop", "seed", "response_schema")


def _params_key(model: str, params: dict[str, Any]) -> str:
    """The namespace part for one model and its request params (ADR 0018 clause 2).

    Every param that shapes the answer is in the key, canonically encoded. It used to be
    ``temperature`` and ``max_tokens`` only, so a request pinning a ``seed``, capping the length
    or asking for a JSON schema could be served an entry made under different ones.
    """
    parts = []
    for name in _KEY_PARAMS:
        if name not in params or params[name] is None:
            continue
        value = params[name]
        if not isinstance(value, str | int | float | bool):
            # A schema (or any structured param): a stable digest, so the key stays short and
            # ordering inside the object cannot split one namespace in two.
            value = hashlib.sha256(
                json.dumps(value, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
        parts.append(f"{name}={value}")
    return f"{model}|" + "|".join(parts)


def namespace(
    model: str, params: dict[str, Any], tenant: str, encoder_id: str | None = None
) -> str:
    """Cache namespace: model + normalized params + tenant (R3) — prevents cross-collision.

    ``encoder_id`` extends the same isolation to the embedding that decides a hit (ADR 0043
    clause 2). This cache matches on cosine similarity, so an entry embedded by one encoder and
    a query embedded by another are compared on axes that have nothing to do with each other —
    and the result is not a miss, it is a *similarity number*, which can clear the 0.85 threshold
    by coincidence and return a cached answer to an unrelated question. Of the three places this
    corruption can occur, the cache is the worst: the vector store returns bad ranking, and the
    cache returns a confident wrong answer with no model call to notice it.

    Handled by **routing, not erroring**. An encoder change should make the old entries invisible
    so the next request recomputes — that is a cache miss, which is a cache working correctly,
    where an exception would be an outage caused by an upgrade.

    Omitted from the key when unset, so every existing caller keeps the namespace it had.
    """
    base = f"{tenant}::{_params_key(model, params)}"
    return f"{base}::enc={encoder_id}" if encoder_id else base


def is_cacheable(
    params: dict[str, Any] | None = None,
    *,
    no_cache: bool = False,
    side_effecting: bool = False,
    bypass_temperature: float = 0.5,
) -> bool:
    """A request bypasses the cache above the temp threshold, on no-cache, or side-effects (R5)."""
    if no_cache or side_effecting:
        return False
    temp = float((params or {}).get("temperature", 0.0))
    return temp <= bypass_temperature


@dataclass
class SemanticCache:
    threshold: float = 0.85  # conservative default (R4)
    ttl_seconds: float = 3600.0
    max_size: int = 1000
    bypass_temperature: float = 0.5
    embed_fn: Callable[[str], list[float]] = _embed
    #: Identity of the encoder behind ``embed_fn`` (ADR 0043). Set it and entries from a previous
    #: encoder stop being reachable; leave it unset and cross-encoder hits remain possible.
    encoder_id: str | None = None
    _entries: list[CacheEntry] = field(default_factory=list)

    def _now(self) -> float:
        return time.time()

    def _evict(self) -> None:
        now = self._now()
        # Drop expired, then trim to max_size (oldest-first).
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        if len(self._entries) > self.max_size:
            self._entries.sort(key=lambda e: e.created_at)
            self._entries = self._entries[-self.max_size :]

    def lookup(
        self, prompt: str, model: str, params: dict[str, Any], tenant: str = "default"
    ) -> tuple[Any | None, float]:
        """Return (completion, similarity) on a hit within threshold + namespace, else (None, best)."""
        self._evict()
        ns = namespace(model, params, tenant, self.encoder_id)
        q = self.embed_fn(prompt)
        best_sim = 0.0
        best: CacheEntry | None = None
        for e in self._entries:
            if e.namespace != ns:  # tenant + params isolation (R3/GWT-3/GWT-4)
                continue
            sim = cosine(q, e.embedding)
            if sim > best_sim:
                best_sim, best = sim, e
        if best is not None and best_sim >= self.threshold:
            self._record(tenant, model, hit=True, similarity=best_sim, entry=best)
            return best.completion, best_sim
        self._record(tenant, model, hit=False, similarity=best_sim, entry=None)
        return None, best_sim

    def store(
        self,
        prompt: str,
        completion: Any,
        model: str,
        params: dict[str, Any],
        tenant: str = "default",
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        ns = namespace(model, params, tenant, self.encoder_id)
        self._entries.append(
            CacheEntry(self.embed_fn(prompt), prompt, completion, ns, self._now(), tokens, cost_usd)
        )
        self._evict()

    def _record(
        self, tenant: str, model: str, *, hit: bool, similarity: float, entry: CacheEntry | None
    ) -> None:
        try:
            from examlops.data.events import record_cache_event

            record_cache_event(
                tenant,
                model,
                hit=hit,
                similarity=similarity,
                tokens_saved=(entry.tokens if (hit and entry) else 0),
                cost_saved=(entry.cost_usd if (hit and entry) else 0.0),
            )
        except Exception:
            pass


def bind_to_gateway(cache: SemanticCache, tenant: str = "default"):
    """Return (cache_lookup, cache_store) callables matching the B2 gateway hook signatures.

    The gateway calls ``cache_lookup(model, messages, params)`` and
    ``cache_store(model, messages, completion, params)``, where ``params`` are the request's own
    sampling params and schema.

    Both of the correctness controls ADR 0018 asks for live here, and both were lost before: the
    binding passed a fixed ``{"temperature": 0.0}``, so every request shared one namespace
    whatever it actually asked for (clause 2), and :func:`is_cacheable` — the temperature bypass
    of clause 3 — was never consulted, so a request asking for variety was answered from cache.
    """

    def _prompt(messages: list) -> str | None:
        """The text the cache keys on, or ``None`` when the request must not be cached.

        A message made of content parts (an image with a question) has no text that identifies
        it: keying on its question alone would hand one chart's answer to a different chart.
        Such requests are always a miss and never stored — they used to crash here instead.
        """
        content = messages[-1].get("content", "") if messages else ""
        return content if isinstance(content, str) else None

    def _params(params: dict[str, Any] | None) -> dict[str, Any] | None:
        """The request's params, or ``None`` when this request must not use the cache."""
        params = dict(params or {})
        if not is_cacheable(params, bypass_temperature=cache.bypass_temperature):
            return None
        return params

    def lookup(model: str, messages: list, params: dict[str, Any] | None = None) -> Any | None:
        prompt, keyed = _prompt(messages), _params(params)
        if prompt is None or keyed is None:
            return None
        comp, _sim = cache.lookup(prompt, model, keyed, tenant)
        return getattr(comp, "text", comp) if comp is not None else None

    def store(
        model: str, messages: list, completion: Any, params: dict[str, Any] | None = None
    ) -> None:
        prompt, keyed = _prompt(messages), _params(params)
        if prompt is None or keyed is None:
            return
        cache.store(
            prompt,
            getattr(completion, "text", completion),
            model,
            keyed,
            tenant,
            tokens=getattr(completion, "completion_tokens", 0),
            cost_usd=getattr(completion, "cost_usd", 0.0),
        )

    return lookup, store
