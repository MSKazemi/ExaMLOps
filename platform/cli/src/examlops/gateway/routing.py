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
    EmbedResult,
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
#: The `cost_aware` strategy's marker cost for every external deployment (ADR 0153 d2) — a nominal
#: non-zero value, not a real price; see `GatewayCore._cost_score`'s docstring for why.
_EXTERNAL_MARKER_COST = 1.0
#: Design spec §5 "Deadline": never start an attempt with less time remaining than this — an
#: attempt with a fraction of a second left cannot plausibly return a useful answer, so failing
#: fast with a clean `upstream_timeout` beats starting a doomed connection.
_DEFAULT_MIN_USEFUL_S = 1.0
#: Design spec §5 "Cold start": the residency-aware version of the floor above — a *known-cold*
#: `ollama` deployment needs its load time too, not just the bare floor. No live load-time
#: measurement is threaded through here (that lives on `ChatResult.load_ms`, only known *after* an
#: attempt completes), so this is a documented, deliberately generous fixed budget, not a measured
#: one — see `GatewayCore.__init__`'s `cold_load_budget_s` for how to override it per deployment.
_DEFAULT_COLD_LOAD_BUDGET_S = 10.0


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
    #: Operator-declared blended price, USD per 1k tokens (ADR 0083/0153 d2). ``None`` = unpriced:
    #: `cost_aware` falls back to locality (local/site free, external a marker cost).
    price_per_1k: float | None = None
    #: Design spec §5 "Cold start": a 1-token keep-alive preload keeps this deployment resident —
    #: an operator's declaration that this model must never pay a cold-load penalty, read by
    #: `GatewayCore.warm_deployments()` (`gateway/health.py`'s `WarmKeeper`).
    warm: bool = False

    @property
    def key(self) -> str:
        return f"{self.provider.name}/{self.model}"


@dataclass
class Route:
    name: str
    deployments: list[Deployment]
    strategy: str = "priority"  # priority | weighted | least_inflight | lowest_latency | cost_aware
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
        min_useful_s: float = _DEFAULT_MIN_USEFUL_S,
        cold_load_budget_s: float = _DEFAULT_COLD_LOAD_BUDGET_S,
    ) -> None:
        self.catalog = catalog
        self._clock = clock
        self._rng = rng or random.Random()
        self.retry_budget = retry_budget or RetryBudget(clock=clock)
        self._breaker_factory = breaker_factory or (
            lambda: CircuitBreaker(clock=clock, rng=self._rng)
        )
        self._states: dict[str, DeploymentState] = {}
        self.min_useful_s = min_useful_s
        self.cold_load_budget_s = cold_load_budget_s
        #: provider name → the model names it last reported resident (ADR 0153's "residency"),
        #: written by an active probe (`gateway/health.py`) via `record_probe_result`. Absent key =
        #: this provider has never reported residency at all (an `openai_compat` router, or an
        #: `ollama` one not yet probed) — permissive, never gates; present key = authoritative for
        #: every model on it, so a model missing from the list is confirmed cold, not merely unknown.
        self._resident: dict[str, list[str]] = {}

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
                    "queue_depth": st.bulkhead.waiting if st.bulkhead else 0,
                    "ewma_ttft_ms": st.ewma_ttft_ms,
                    "ok": st.ok,
                    "errors": st.errors,
                    "last_error": st.last_error,
                }
        return out

    def deployments_for_provider(self, provider_name: str) -> list[Deployment]:
        """Every deployment (across every route) backed by ``provider_name`` — a provider is
        reused across many routes/models, but health is a property of the *provider connection*,
        not any one route, so an out-of-band signal about it (:meth:`record_probe_result`) must
        reach every deployment that shares it, not just the one route that happened to ask."""
        return [
            dep
            for route in self.catalog.routes.values()
            for dep in route.deployments
            if dep.provider.name == provider_name
        ]

    def warm_deployments(self) -> list[Deployment]:
        """Every deployment flagged ``warm: true`` (design spec §5 "Cold start"), deduplicated by
        :attr:`Deployment.key` — the same (provider, model) pair can appear under more than one
        route name, and a keep-alive ping only needs to be sent once per real upstream target, not
        once per route that happens to reference it."""
        seen: dict[str, Deployment] = {}
        for route in self.catalog.routes.values():
            for dep in route.deployments:
                if dep.warm:
                    seen.setdefault(dep.key, dep)
        return list(seen.values())

    def record_probe_result(
        self, provider_name: str, ok: bool, *, resident: list[str] | None = None
    ) -> None:
        """Feed an out-of-band health probe's result into the breaker of every deployment this
        provider backs — the write side of the active probe loop (``gateway/health.py``,
        PLAN.md P1's "active probe loop"). A probe failure counts toward the breaker exactly like
        a real request's classified failure would, so a dead upstream can be caught *before* the
        next real request reaches it, not only after one fails and pays the cost of finding out.

        Deliberately breaker-only: it does not touch ``DeploymentState.ok``/``errors``/
        ``last_error``/``ewma_ttft_ms`` — those describe *real request* outcomes (latency,
        counts) for the admin/metrics surface, and blending a probe's synthetic result into them
        would make "how many real requests succeeded" a lie. The breaker is the one piece of
        state this is legitimately shared infrastructure for: it already exists to answer
        "is this deployment currently healthy", regardless of who's asking.

        ``resident``, when the provider's own probe reports it (``ollama`` does; ``openai_compat``
        never does), feeds :meth:`_min_useful_s`'s residency-aware deadline gate (design spec §5
        "Cold start") — ``None`` leaves whatever this provider last reported untouched, so a
        transient probe that cannot determine residency (e.g. the discovery call itself failed)
        does not erase a still-valid earlier reading.
        """
        for dep in self.deployments_for_provider(provider_name):
            st = self._state(dep)
            if ok:
                st.breaker.record_success()
            else:
                st.breaker.record_failure()
        if resident is not None:
            self._resident[provider_name] = resident

    def _min_useful_s(self, dep: Deployment) -> float:
        """The design spec's `min_useful` (§5 "Deadline"): never start an attempt with less time
        remaining than this. Residency-aware for `ollama` deployments only (§5 "Cold start" is
        explicitly scoped to `ollama`'s own `/api/ps` residency — no other provider type reports
        it, and a false "cold" reading here means skipping a candidate that might have answered
        fine). Absent residency data for the provider (never probed yet, or a non-`ollama` type)
        is permissive — the bare floor, not the cold-load budget."""
        if dep.provider.type != "ollama":
            return self.min_useful_s
        resident = self._resident.get(dep.provider.name)
        if resident is None or dep.model in resident:
            return self.min_useful_s
        return self.cold_load_budget_s

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
            elif route.strategy == "cost_aware":
                # Higher score wins (the llm_routing provider's caller "maximizes"); reverse=True.
                group.sort(key=self._cost_score, reverse=True)
            ordered.extend(group)
        return ordered

    def _cost_score(self, dep: Deployment) -> float:
        """A deployment's `cost_aware` ranking score (ADR 0153 d2) via the ``llm_routing`` provider.

        Cost signal, most specific first: the deployment's own ``price_per_1k`` from
        `gateway.yaml` when the operator declared one (a per-1k-token price is comparable across
        deployments before a request's token count is known); otherwise locality — "FinOps rate,
        local = 0 marginal" (the ADR's own words): every local/site deployment costs nothing and
        every external one is charged the same non-zero marker cost. Ties keep the group's existing
        order (Python's stable sort). The deployment's observed TTFT EWMA is handed in as
        ``latency_ms`` (unknown = 0, i.e. explore, as `lowest_latency` does), so selecting the
        ``cost-latency`` provider (ADR 0083) trades price against speed with no code change.
        """
        from examlops.llmops_providers import route_score_via_provider

        if dep.price_per_1k is not None:
            cost = dep.price_per_1k
        else:
            cost = 0.0 if dep.provider.locality in ("local", "site") else _EXTERNAL_MARKER_COST
        latency = self._state(dep).ewma_ttft_ms
        return route_score_via_provider(cost_usd=cost, healthy=True, latency_ms=latency)

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
        dep: Deployment,
    ) -> None:
        """Gate every attempt, including the first: enough time must remain to plausibly get a
        useful answer from `dep` specifically (residency-aware, :meth:`_min_useful_s`) — a caller
        can hand in an already-short `X-ExaMLOps-Budget-Ms`, so this is not only a failover
        concern. Every attempt *after* the first also needs the retry budget."""
        if deadline.remaining() < self._min_useful_s(dep):
            raise _error(
                "upstream_timeout",
                f"{dep.key}: not enough of the deadline remains to plausibly get a useful answer",
                attempts,
            )
        if previous is None:
            return
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
            self._may_start(last, deadline, attempts, dep)
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

    # ── embeddings ───────────────────────────────────────────────────────────
    #
    # ADR 0152 d1 declared `Provider.embed()` and `OllamaProvider` has implemented it since
    # 2026-09-20; nothing routed to it. This reuses every resilience primitive `chat` does
    # (breaker, retry budget, bulkhead, locality, deadline) — the *only* difference from `chat` is
    # what "capable" means (an ``embeddings`` flag, not tools/vision/context) and what the
    # provider is asked to do — never a smaller-scoped, separately-trusted code path.

    def _candidates_embed(
        self, model: str, allowed: tuple[str, ...]
    ) -> tuple[list[Deployment], dict[str, Any]]:
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
                # Declared capabilities are permissive when unknown (`None`) — same rule as chat's
                # `_missing()`: an undeclared deployment is never assumed incapable.
                caps = dep.capabilities
                if loc not in allowed or (loc == "external" and not dep.external_ok):
                    dropped["locality"] += 1
                elif caps is not None and not caps.embeddings:
                    dropped["capability"].append("embeddings")
                elif not self._state(dep).breaker.would_allow():
                    dropped["breaker"] += 1
                else:
                    eligible.append(dep)
            out.extend(self._order(route, eligible))
        return out, dropped

    async def _attempt_embed(
        self, dep: Deployment, inputs: list[str], deadline: Deadline
    ) -> EmbedResult:
        st = self._state(dep)
        slot = st.bulkhead.slot() if st.bulkhead else contextlib.nullcontext()
        async with slot:  # type: ignore[attr-defined]
            if not st.breaker.allow():
                raise _Skipped
            st.inflight += 1
            started = self._clock()
            try:
                async with asyncio.timeout(deadline.remaining()):
                    result = await dep.provider.embed(dep.model, inputs)
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
            self._note_success(st, None, (self._clock() - started) * 1000.0)
            return result

    async def embed(
        self,
        model: str,
        inputs: list[str],
        *,
        allowed_localities: tuple[str, ...] = DEFAULT_LOCALITIES,
        caller_budget_ms: float | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> EmbedResult:
        attempts = attempts if attempts is not None else []
        route = self._chain(model)[0]
        deadline = Deadline.from_budget(
            route.total_timeout_s, caller_ms=caller_budget_ms, clock=self._clock
        )
        self.retry_budget.note_request()
        cands, dropped = self._candidates_embed(model, allowed_localities)
        if not cands:
            raise self._none_available(model, dropped, allowed_localities, attempts)
        last: ProviderError | None = None
        for dep in cands:
            self._may_start(last, deadline, attempts, dep)
            started = self._clock()
            try:
                result = await self._attempt_embed(dep, inputs, deadline)
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
            self._may_start(last, deadline, attempts, dep)
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
