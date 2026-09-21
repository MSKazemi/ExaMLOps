"""The routing core (ADR 0153): candidates → order → bulkhead → breaker → attempt → classify.

``GatewayCore`` is the async data plane. It owns *no* policy (keys, budgets, guardrails, cache):
those wrap it. What it decides is which deployment serves a request and what happens when one
fails — and every rule here is one of the ADR's, in the ADR's words:

* locality is filtered **before** selection and again for every fallback, so a fallback can never
  widen where a prompt may go (ADR 0154 d3);
* a client mistake is raised, never failed over; an upstream fault is (ADR 0153 d4);
* failovers spend a shared retry budget, so one bad upstream cannot become a storm (d5);
* a full local queue is capacity, not a fault: it fails over free and never trips a breaker (d6);
* once the first byte of a stream has gone out, the stream is never re-routed (d9).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import random
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from examlops.gateway.providers.base import (
    Capabilities,
    ChatChunk,
    ChatRequest,
    ChatResult,
    Provider,
    ProviderError,
)
from examlops.gateway.resilience import Bulkhead, CircuitBreaker, Deadline, RetryBudget

DEFAULT_LOCALITIES = ("local", "site")

#: Outcomes that say the upstream itself is unhealthy. Everything else (a 400, a 429, a missing
#: model) proves the server is answering and must not open a breaker.
_TRIPS = frozenset(
    {"upstream_unavailable", "upstream_timeout", "upstream_error", "stream_interrupted"}
)
#: Free failovers: capacity and per-deployment configuration, not load the upstream is failing under.
_FREE_FAILOVER = frozenset({"queue_full", "model_not_found"})


@dataclass
class Deployment:
    """One (provider, upstream model) a route may send requests to."""

    provider: Provider
    model: str
    weight: float = 1.0
    priority: int = 0  # lower is tried first; the strategy orders within a priority level
    #: ``None`` means *unknown* and is permissive — discovery or the operator fills it in.
    capabilities: Capabilities | None = None
    #: The deployment-side half of ADR 0154 d3: an external provider is never used without it.
    external_ok: bool = False
    max_concurrency: int | None = None
    max_queue: int = 8
    queue_timeout_s: float = 5.0

    @property
    def key(self) -> str:
        return f"{self.provider.name}/{self.model}"


@dataclass
class Route:
    name: str
    deployments: list[Deployment]
    strategy: str = "priority"  # priority | weighted | least_inflight | lowest_latency
    fallbacks: list[str] = field(default_factory=list)
    total_timeout_s: float = 300.0
    required: bool = False


class Catalog:
    def __init__(self, routes: list[Route], aliases: dict[str, str] | None = None) -> None:
        self.routes = {r.name: r for r in routes}
        self.aliases = dict(aliases or {})

    def resolve(self, name: str) -> Route | None:
        return self.routes.get(self.aliases.get(name, name))


@dataclass
class DeploymentState:
    breaker: CircuitBreaker
    bulkhead: Bulkhead | None = None
    inflight: int = 0
    ewma_ttft_ms: float | None = None
    ok: int = 0
    errors: int = 0
    last_error: str = ""


def _needs(req: ChatRequest) -> dict[str, Any]:
    chars, vision = 0, False
    for m in req.messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    vision = True
                chars += len(str(part.get("text", "")))
        else:
            chars += len(str(content or ""))
    return {"tools": bool(req.tools), "vision": vision, "tokens": chars // 4}


def _missing(dep: Deployment, needs: dict[str, Any]) -> str | None:
    caps = dep.capabilities
    if caps is None:
        return None
    lacking = []
    if not caps.chat:
        lacking.append("chat")
    if needs["tools"] and not caps.tools:
        lacking.append("tools")
    if needs["vision"] and not caps.vision:
        lacking.append("vision")
    if caps.context_window and needs["tokens"] > caps.context_window:
        lacking.append(f"context window (~{needs['tokens']} tokens > {caps.context_window})")
    return ", ".join(lacking) or None


def _error(kind: str, message: str, attempts: list[dict[str, Any]], **kw: Any) -> ProviderError:
    err = ProviderError(kind, message, **kw)
    err.attempts = attempts
    return err


class GatewayCore:
    def __init__(
        self,
        catalog: Catalog,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        retry_budget: RetryBudget | None = None,
        breaker_factory: Callable[[], CircuitBreaker] | None = None,
    ) -> None:
        self.catalog = catalog
        self._clock = clock
        self._rng = rng or random.Random()
        self.retry_budget = retry_budget or RetryBudget(clock=clock)
        self._breaker_factory = breaker_factory or (
            lambda: CircuitBreaker(clock=clock, rng=self._rng)
        )
        self._states: dict[str, DeploymentState] = {}

    # ── state ────────────────────────────────────────────────────────────────

    def _state(self, dep: Deployment) -> DeploymentState:
        st = self._states.get(dep.key)
        if st is None:
            bulkhead = (
                Bulkhead(dep.max_concurrency, dep.max_queue, dep.queue_timeout_s)
                if dep.max_concurrency is not None
                else None
            )
            st = self._states[dep.key] = DeploymentState(self._breaker_factory(), bulkhead)
        return st

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-deployment health for the admin surface and metrics."""
        out: dict[str, dict[str, Any]] = {}
        for route in self.catalog.routes.values():
            for dep in route.deployments:
                st = self._state(dep)
                out[dep.key] = {
                    "breaker": st.breaker.state,
                    "open_remaining_s": round(st.breaker.open_remaining(), 3),
                    "inflight": st.inflight,
                    "ewma_ttft_ms": st.ewma_ttft_ms,
                    "ok": st.ok,
                    "errors": st.errors,
                    "last_error": st.last_error,
                }
        return out

    # ── candidate selection ──────────────────────────────────────────────────

    def _chain(self, model: str) -> list[Route]:
        first = self.catalog.resolve(model)
        if first is None:
            raise ProviderError("model_not_found", f"no route named {model!r}")
        chain, queue = [], [first]
        while queue:  # breadth-first over fallbacks, cycle-safe
            route = queue.pop(0)
            if route in chain:
                continue
            chain.append(route)
            queue.extend(r for n in route.fallbacks if (r := self.catalog.resolve(n)) is not None)
        return chain

    def _order(self, route: Route, deps: list[Deployment]) -> list[Deployment]:
        ordered: list[Deployment] = []
        for level in sorted({d.priority for d in deps}):
            group = [d for d in deps if d.priority == level]
            if route.strategy == "weighted":
                # Weighted random permutation (Efraimidis–Spirakis): seedable, so tests are exact.
                group.sort(
                    key=lambda d: self._rng.random() ** (1.0 / max(d.weight, 1e-9)), reverse=True
                )
            elif route.strategy == "least_inflight":
                group.sort(key=lambda d: self._state(d).inflight)
            elif route.strategy == "lowest_latency":
                group.sort(key=lambda d: self._state(d).ewma_ttft_ms or 0.0)  # unknown = 0: explore
            ordered.extend(group)
        return ordered

    def _candidates(
        self, model: str, req: ChatRequest, allowed: tuple[str, ...]
    ) -> tuple[list[Deployment], dict[str, Any]]:
        needs = _needs(req)
        dropped: dict[str, Any] = {"locality": 0, "capability": [], "breaker": 0}
        out: list[Deployment] = []
        seen: set[str] = set()
        for route in self._chain(model):
            eligible = []
            for dep in route.deployments:
                if dep.key in seen:
                    continue
                seen.add(dep.key)
                loc = dep.provider.locality
                if loc not in allowed or (loc == "external" and not dep.external_ok):
                    dropped["locality"] += 1
                elif (why := _missing(dep, needs)) is not None:
                    dropped["capability"].append(why)
                elif not self._state(dep).breaker.would_allow():
                    dropped["breaker"] += 1
                else:
                    eligible.append(dep)
            out.extend(self._order(route, eligible))
        return out, dropped

    @staticmethod
    def _none_available(
        model: str,
        dropped: dict[str, Any],
        allowed: tuple[str, ...],
        attempts: list[dict[str, Any]],
    ) -> ProviderError:
        if dropped["capability"]:
            return _error(
                "capability_unavailable",
                f"no deployment of {model!r} supports: {dropped['capability'][0]}",
                attempts,
            )
        if dropped["locality"] and not dropped["breaker"]:
            return _error(
                "locality_denied",
                f"no deployment of {model!r} is permitted for locality {list(allowed)}",
                attempts,
            )
        if dropped["breaker"]:
            return _error(
                "upstream_unavailable",
                f"no healthy deployment for {model!r} (circuit open)",
                attempts,
            )
        return _error("upstream_unavailable", f"route {model!r} has no deployments", attempts)

    # ── outcomes ─────────────────────────────────────────────────────────────

    def _note_failure(self, st: DeploymentState, err: ProviderError) -> None:
        st.errors += 1
        st.last_error = str(err)[:300]
        if err.kind in _TRIPS:
            st.breaker.record_failure()
        else:
            st.breaker.record_success()  # it answered; releases a half-open trial slot

    def _note_success(self, st: DeploymentState, ttft_ms: float | None, elapsed_ms: float) -> None:
        st.ok += 1
        st.breaker.record_success()
        sample = ttft_ms if ttft_ms is not None else elapsed_ms
        st.ewma_ttft_ms = (
            sample if st.ewma_ttft_ms is None else 0.7 * st.ewma_ttft_ms + 0.3 * sample
        )

    @staticmethod
    def _fails_over(err: ProviderError) -> bool:
        return err.retryable or err.kind in _FREE_FAILOVER or err.kind == "upstream_error"

    def _may_start(
        self,
        previous: ProviderError | None,
        deadline: Deadline,
        attempts: list[dict[str, Any]],
    ) -> None:
        """Gate every attempt after the first: time must remain and the retry budget must allow it."""
        if previous is None:
            return
        if deadline.expired:
            raise _error("upstream_timeout", "request deadline exhausted", attempts)
        if previous.kind not in _FREE_FAILOVER and not self.retry_budget.try_retry():
            raise _error(
                "upstream_unavailable",
                f"retry budget exhausted after: {previous}",
                attempts,
                retry_after=1.0,
            )

    # ── non-streaming ────────────────────────────────────────────────────────

    async def _attempt(self, dep: Deployment, req: ChatRequest, deadline: Deadline) -> ChatResult:
        st = self._state(dep)
        slot = st.bulkhead.slot() if st.bulkhead else contextlib.nullcontext()
        async with slot:  # type: ignore[attr-defined]
            if not st.breaker.allow():
                raise _Skipped
            st.inflight += 1
            started = self._clock()
            try:
                async with asyncio.timeout(deadline.remaining()):
                    result = await dep.provider.chat(dataclasses.replace(req, model=dep.model))
            except TimeoutError:
                timeout_err = ProviderError(
                    "upstream_timeout",
                    f"{dep.key} did not answer within the request deadline",
                    provider=dep.provider.name,
                )
                self._note_failure(st, timeout_err)
                raise timeout_err from None
            except ProviderError as err:
                self._note_failure(st, err)
                raise
            except Exception as exc:  # noqa: BLE001 - a provider bug must not escape the error contract
                wrapped = ProviderError(
                    "upstream_error", f"{type(exc).__name__}: {exc}", provider=dep.provider.name
                )
                self._note_failure(st, wrapped)
                raise wrapped from exc
            finally:
                st.inflight -= 1
            self._note_success(st, result.ttft_ms, (self._clock() - started) * 1000.0)
            return result

    async def chat(
        self,
        model: str,
        req: ChatRequest,
        *,
        allowed_localities: tuple[str, ...] = DEFAULT_LOCALITIES,
        caller_budget_ms: float | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        attempts = attempts if attempts is not None else []
        route = self._chain(model)[0]
        deadline = Deadline.from_budget(
            route.total_timeout_s, caller_ms=caller_budget_ms, clock=self._clock
        )
        self.retry_budget.note_request()
        cands, dropped = self._candidates(model, req, allowed_localities)
        if not cands:
            raise self._none_available(model, dropped, allowed_localities, attempts)
        last: ProviderError | None = None
        for dep in cands:
            self._may_start(last, deadline, attempts)
            started = self._clock()
            try:
                result = await self._attempt(dep, req, deadline)
            except _Skipped:
                continue
            except ProviderError as err:
                attempts.append(_record(dep, err.kind, started, self._clock()))
                err.attempts = attempts
                if not self._fails_over(err):
                    raise
                last = err
                continue
            attempts.append(_record(dep, "ok", started, self._clock()))
            return result
        if last is None:  # every candidate's breaker closed on us between filtering and attempting
            raise self._none_available(
                model, {"breaker": 1, "locality": 0, "capability": []}, allowed_localities, attempts
            )
        raise last

    # ── streaming ────────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        model: str,
        req: ChatRequest,
        *,
        allowed_localities: tuple[str, ...] = DEFAULT_LOCALITIES,
        caller_budget_ms: float | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[ChatChunk, None]:
        attempts = attempts if attempts is not None else []
        route = self._chain(model)[0]
        deadline = Deadline.from_budget(
            route.total_timeout_s, caller_ms=caller_budget_ms, clock=self._clock
        )
        self.retry_budget.note_request()
        cands, dropped = self._candidates(model, req, allowed_localities)
        if not cands:
            raise self._none_available(model, dropped, allowed_localities, attempts)
        last: ProviderError | None = None
        for dep in cands:
            self._may_start(last, deadline, attempts)
            st = self._state(dep)
            started = self._clock()
            committed = False
            rec: dict[str, Any] | None = None  # the serving attempt, once a token has gone out
            agen = None
            try:
                slot = st.bulkhead.slot() if st.bulkhead else contextlib.nullcontext()
                async with slot:  # type: ignore[attr-defined]
                    if not st.breaker.allow():
                        continue
                    st.inflight += 1
                    try:
                        agen = dep.provider.chat_stream(dataclasses.replace(req, model=dep.model))
                        try:
                            async with asyncio.timeout(deadline.remaining()):
                                first = await anext(agen, None)
                        except TimeoutError:
                            raise ProviderError(
                                "upstream_timeout",
                                f"{dep.key} produced no first token within the deadline",
                                provider=dep.provider.name,
                            ) from None
                        committed = True  # from here the stream is the caller's, never re-routed
                        rec = _record(dep, "streaming", started, self._clock())
                        attempts.append(rec)
                        ttft_ms = (self._clock() - started) * 1000.0
                        if first is not None:
                            yield first
                        async for chunk in agen:
                            yield chunk
                        self._note_success(st, ttft_ms, ttft_ms)
                        rec.update(_record(dep, "ok", started, self._clock()))
                        return
                    finally:
                        st.inflight -= 1
                        close = getattr(agen, "aclose", None)
                        if close is not None:
                            with contextlib.suppress(Exception):
                                await close()
            except ProviderError as err:
                self._note_failure(st, err)
                final = _record(dep, err.kind, started, self._clock())
                if rec is not None:
                    rec.update(final)
                else:
                    attempts.append(final)
                err.attempts = attempts
                if committed or not self._fails_over(err):
                    raise
                last = err
        if last is None:
            raise self._none_available(
                model, {"breaker": 1, "locality": 0, "capability": []}, allowed_localities, attempts
            )
        raise last


class _Skipped(Exception):
    """The breaker closed on a candidate between filtering and the attempt: not an attempt."""


def _record(dep: Deployment, outcome: str, started: float, now: float) -> dict[str, Any]:
    return {
        "provider": dep.provider.name,
        "model": dep.model,
        "outcome": outcome,
        "ms": round((now - started) * 1000.0, 1),
    }
