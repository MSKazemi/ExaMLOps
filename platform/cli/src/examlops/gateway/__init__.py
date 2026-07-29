"""B2 — Model gateway & multi-provider routing (ADR 0010).

One OpenAI-compatible entry point in front of all LLM backends, with weighted routing +
failover, per-tenant/project **virtual keys** (allow-list + budget), per-call C1 span +
FinOps cost, and a B3 semantic-cache hook. The production gateway is LiteLLM; this module
is the **client + policy layer** that works standalone (backends are callables), so the
routing/governance/cost logic is exercisable with no external service.

Consumers (Skipper, the C2 judge, RAG, served LLMs) call :class:`GatewayClient`, which
degrades to a configured last-resort backend if the gateway is unreachable (R11).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class GatewayError(RuntimeError):
    """Base class for typed gateway errors (never surfaced as a generic 500)."""


class BudgetExceeded(GatewayError):
    """The virtual key is over its budget (R6)."""


class ModelNotAllowed(GatewayError):
    """The virtual key is not allow-listed for the requested model (R7)."""


class KeyInvalid(GatewayError):
    """Unknown or revoked virtual key (R5)."""


class AllBackendsFailed(GatewayError):
    """Every routed backend errored and no fallback succeeded (R3)."""


@dataclass
class Completion:
    text: str
    model: str
    backend: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False


# A backend is any callable: (model, messages, **kw) -> Completion-ish.
Backend = Callable[..., Any]


@dataclass
class Route:
    """A logical model → ordered concrete backends with weights and fallbacks."""

    logical: str
    backends: list[tuple[str, Backend]]  # (name, callable), in priority order

    def ordered(self) -> list[tuple[str, Backend]]:
        return list(self.backends)


@dataclass
class Router:
    """Config-driven routing table; hot-reloadable by replacing ``routes`` (R2)."""

    routes: dict[str, Route] = field(default_factory=dict)

    def add_route(self, logical: str, backends: list[tuple[str, Backend]]) -> None:
        self.routes[logical] = Route(logical, backends)

    def resolve(self, model: str) -> Route | None:
        return self.routes.get(model)


# ── Cost estimation (reuses C1's rate table when available) ───────────────────


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    try:
        from examlops.telemetry.genai import estimate_cost

        return estimate_cost(model, prompt_tokens, completion_tokens)
    except Exception:
        # Fallback flat rate: $0.5 / 1M input, $1.5 / 1M output.
        return prompt_tokens * 0.5e-6 + completion_tokens * 1.5e-6


# ── Virtual keys (admin) ──────────────────────────────────────────────────────


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_virtual_key(
    tenant: str,
    project: str,
    models: list[str] | None,
    budget_usd: float | None,
    actor: str,
) -> str:
    """Issue a virtual key scoped to a tenant/project (+ allow-list + budget). Audited (R5)."""
    from examlops.data.audit import write_audit_event
    from examlops.data.gateway import create_virtual_key

    raw = "exa-" + secrets.token_urlsafe(24)
    create_virtual_key(
        _hash_key(raw),
        tenant=tenant,
        project=project,
        models=models,
        budget_usd=budget_usd,
        created_by=actor,
    )
    write_audit_event(
        "exa-gateway",
        actor,
        "virtual_key_issued",
        f"{tenant}/{project}",
        {"models": models or "all", "budget_usd": budget_usd},
    )
    return raw


def authorize(key_raw: str, model: str) -> dict[str, Any]:
    """Validate a key for a model (existence, revocation, allow-list, budget). Raises on deny."""
    from examlops.data.gateway import get_virtual_key

    rec = get_virtual_key(_hash_key(key_raw))
    if rec is None or rec.get("revoked"):
        raise KeyInvalid("unknown or revoked virtual key")
    allow = rec.get("models") or []
    if allow and model not in allow:
        raise ModelNotAllowed(f"key not allow-listed for model '{model}'")
    budget = rec.get("budget_usd")
    if budget is not None and rec.get("spent_usd", 0.0) >= budget:
        raise BudgetExceeded(
            f"budget ${budget:.4f} exhausted (spent ${rec.get('spent_usd', 0):.4f})"
        )
    return rec


# ── Client ────────────────────────────────────────────────────────────────────


@dataclass
class GatewayClient:
    """Thin OpenAI-compatible client with routing, failover, budget, cost + telemetry."""

    router: Router
    virtual_key: str | None = None
    tenant: str = "default"
    last_resort: Backend | None = None  # R11 degrade path
    cache_lookup: Callable[[str, list], Any] | None = None  # B3 hook
    cache_store: Callable[[str, list, Completion], None] | None = None

    def chat(self, model: str, messages: list[dict[str, str]], **kw: Any) -> Completion:
        from examlops.data.finops import add_key_spend
        from examlops.data.gateway import record_gateway_call

        key_hash = _hash_key(self.virtual_key) if self.virtual_key else None
        if self.virtual_key:
            authorize(self.virtual_key, model)  # raises typed errors before any backend call

        # B3 semantic-cache hook (optional; caller API unchanged, R9).
        if self.cache_lookup is not None:
            hit = self.cache_lookup(model, messages)
            if hit is not None:
                return Completion(text=hit, model=model, backend="cache", cached=True)

        route = self.router.resolve(model)
        candidates = route.ordered() if route else []
        if self.last_resort is not None:
            candidates = candidates + [("last-resort", self.last_resort)]

        errors: list[str] = []
        for name, backend in candidates:
            try:
                raw = backend(model, messages, **kw)
                comp = _coerce(raw, model, name)
            except Exception as exc:  # failover to the next backend (R3)
                errors.append(f"{name}: {exc}")
                continue
            comp.cost_usd = comp.cost_usd or _estimate_cost(
                model, comp.prompt_tokens, comp.completion_tokens
            )
            # C1 span (best-effort) + FinOps cost (R8).
            _emit_span(model, self.tenant, comp)
            record_gateway_call(
                key_hash,
                model,
                backend=comp.backend,
                cost_usd=comp.cost_usd,
                prompt_tokens=comp.prompt_tokens,
                completion_tokens=comp.completion_tokens,
            )
            if key_hash:
                add_key_spend(key_hash, comp.cost_usd)
            if self.cache_store is not None:
                self.cache_store(model, messages, comp)
            return comp

        raise AllBackendsFailed("; ".join(errors) or f"no backend for model '{model}'")

    def health(self) -> dict[str, bool]:
        """Readiness of every routed backend (R-A3), reachable through the gateway.

        A local-engine backend (see :func:`engine_backend`) exposes its engine's
        ``health()``; backends without a probe are reported ready.
        """
        out: dict[str, bool] = {}
        for logical, route in self.router.routes.items():
            for name, backend in route.ordered():
                probe = getattr(backend, "health", None)
                try:
                    out[f"{logical}:{name}"] = bool(probe()) if callable(probe) else True
                except Exception:  # a probe must never break the health surface
                    out[f"{logical}:{name}"] = False
        return out


def _coerce(raw: Any, model: str, backend: str) -> Completion:
    if isinstance(raw, Completion):
        raw.backend = raw.backend or backend
        return raw
    if isinstance(raw, dict):
        return Completion(
            text=str(raw.get("text", raw.get("content", ""))),
            model=model,
            backend=backend,
            prompt_tokens=int(raw.get("prompt_tokens", 0)),
            completion_tokens=int(raw.get("completion_tokens", 0)),
            cost_usd=float(raw.get("cost_usd", 0.0)),
        )
    return Completion(text=str(raw), model=model, backend=backend)


def _emit_span(model: str, tenant: str, comp: Completion) -> None:
    try:
        from examlops.telemetry import genai

        with genai.genai_span("chat", system="gateway", model=model, tenant=tenant) as span:
            genai.record_usage(
                span,
                model=model,
                input_tokens=comp.prompt_tokens,
                output_tokens=comp.completion_tokens,
            )
    except Exception:
        pass


def build_default_router() -> Router:
    """Router seeded from env; falls back to a local echo backend so it always works."""

    def _echo(model: str, messages: list[dict[str, str]], **kw: Any) -> Completion:
        last = messages[-1]["content"] if messages else ""
        return Completion(text=last, model=model, backend="echo", prompt_tokens=len(last.split()))

    router = Router()
    default_model = os.getenv("EXAMLOPS_GATEWAY_DEFAULT_MODEL", "default")
    router.add_route(default_model, [("echo", _echo)])
    return router


# ── Local-engine edge (R-A1: gateway → engines.build_engine) ───────────────────
#
# The single wiring from the B2 gateway into the E2 engine layer. The dependency
# direction is strictly gateway → engines (``engines`` never imports ``gateway``),
# so a locally-hosted model (vLLM on GPU, echo on CPU/CI) becomes a first-class
# gateway backend with routing, keys, budgets, cost + telemetry applied around it.

_SAMPLING_KEYS = ("temperature", "max_tokens", "top_p", "stop", "seed")


def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
    """Flatten OpenAI-style chat messages into a single prompt string for an engine."""
    return "\n".join(str(m.get("content", "")) for m in messages).strip()


def engine_backend(model_name: str, config: Any = None) -> Backend:
    """R-A1: a gateway :data:`Backend` that dispatches to a local ``InferenceEngine``.

    ``config`` may be an ``engines.EngineConfig``, a plain ``dict`` (per-model YAML
    ``engine:`` block), or ``None`` (defaults). On a CPU/CI host with no vLLM the
    engine degrades to ``EchoEngine`` (R-A8), so the full gateway→engine path is
    exercisable with no GPU. The returned callable carries ``.health`` (reachable via
    :meth:`GatewayClient.health`) and ``.engine`` for introspection.
    """
    from examlops.engines import EngineConfig, build_engine

    cfg = config if isinstance(config, EngineConfig) else EngineConfig()
    if isinstance(config, dict):
        cfg = EngineConfig.from_dict(config)
    engine = build_engine(cfg, model_path=model_name)

    def _backend(model: str, messages: list[dict[str, str]], **kw: Any) -> Completion:
        prompt = _messages_to_prompt(messages)
        sampling = {k: v for k, v in kw.items() if k in _SAMPLING_KEYS}
        ec = engine.generate(prompt, **sampling)
        return Completion(
            text=ec.text,
            model=model,
            backend=engine.name,
            prompt_tokens=ec.prompt_tokens,
            completion_tokens=ec.completion_tokens,
        )

    _backend.health = engine.health  # type: ignore[attr-defined]
    _backend.engine = engine  # type: ignore[attr-defined]
    return _backend


def build_engine_router(
    model_name: str, config: Any = None, *, logical: str | None = None
) -> Router:
    """Router serving one logical model from a local engine, with echo last-resort (R-A1/R-A8)."""
    router = Router()
    router.add_route(
        logical or model_name, [(f"engine:{model_name}", engine_backend(model_name, config))]
    )
    return router
