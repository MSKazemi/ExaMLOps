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
import json
import os
import re
import secrets
import time
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


class GuardrailBlocked(GatewayError):
    """A D8 guardrail blocked the request or the response (ADR 0026 clause 3).

    Like :class:`MediaNotAllowed` this is a **policy denial, not a backend failure**, so it is
    never retried against the next backend: a second backend would deny an injected prompt
    identically, and retrying an output block would spend money to produce the same violation.
    """

    def __init__(self, direction: str, findings: list[str], reason: str) -> None:
        self.direction = direction
        self.findings = findings
        super().__init__(f"guardrail blocked the {direction}: {reason} ({', '.join(findings)})")


class MediaNotAllowed(GatewayError):
    """A multimodal content part failed validation before dispatch (R-V5).

    Wraps ``engines.media.MediaRejected`` so callers can catch one gateway error family.
    Raised *before* any backend call — a rejected image never reaches an engine.
    """


@dataclass
class Completion:
    text: str
    model: str
    backend: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    #: The schema-validated object, when ``chat(..., response_schema=…)`` was used (ADR 0035
    #: clause 1). ``None`` means no schema was requested — never "it failed", which raises.
    parsed: Any = None


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
    """Cost for one generation, through the swappable ``llm_cost`` provider (ADR 0083).

    Order: the operator-selected ``llm_cost`` provider, then C1's built-in rate table, then
    a flat fallback. Going through the provider first is what lets a site override the rate
    model (per-token contract pricing, on-prem amortisation) without touching core code —
    previously this path called C1 directly and the provider seam was dead on the live path.
    """
    try:
        from examlops.llmops_providers import estimate_llm_cost_via_provider

        cost = estimate_llm_cost_via_provider(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        if cost is not None:
            return float(cost)
    except Exception:
        pass
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
    *,
    source: str = "exa-gateway",
) -> str:
    """Issue a virtual key scoped to a tenant/project (+ allow-list + budget). Audited (R5).

    ``source`` attributes the audit event to the calling surface (``"exa-gateway"`` by default; the
    dashboard passes ``"dashboard"``) so this shared issuance path serves every face of the platform.
    """
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
        source,
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


def resolve_prompt_ref(ref: str) -> tuple[str, str, int]:
    """Resolve ``name`` or ``name@label`` to (template, name, version) — ADR 0009 clause 3.

    Goes through :func:`examlops.prompts.get_prompt`, so serving shares Skipper's client:
    the same short-TTL cache and the same last-known-good fallback when the registry is
    briefly unreachable.

    Unlike Skipper, an unresolvable reference is an **error, not a fallback**. Skipper has a
    literal that is always a correct system prompt; a caller who names ``support-bot@prod``
    has no such default, and silently sending the request without it would change the
    model's behaviour invisibly. Failing loudly is the only honest option.
    """
    from examlops.prompts import get_prompt

    name, _, label = ref.partition("@")
    pv = get_prompt(name, label or "prod")
    return pv.template, pv.name, pv.version


# ── A2/E2 structured output at the gateway (ADR 0035 clause 1) ───────────────

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Parse the object out of a completion, or raise ``ValueError``.

    Tolerant of the one thing every instruction-tuned model does regardless of the prompt:
    wrapping the object in a ``` fence, often with a line of prose above it. Without this the
    platform would report a schema failure for a response that contains a perfectly good object,
    and the repair path would be spent fixing a formatting habit rather than a data problem.
    """
    raw = text.strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    fenced = _FENCE.search(raw)
    if fenced:
        return json.loads(fenced.group(1).strip())
    # Last resort: the outermost {...} or [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            return json.loads(raw[start : end + 1])
    raise ValueError("no JSON object found in the response")


def _enforce_schema(
    comp: Completion,
    schema: dict[str, Any],
    *,
    tenant: str,
    max_repairs: int,
    constrained: bool = False,
) -> None:
    """Attach a schema-valid object to ``comp``, or raise ``StructuredOutputError``.

    ADR 0035 clause 1 asks the platform to *guarantee* a response validates, and it is reached
    from both ends. When the backend can constrain its decoder the schema went *into* the request
    (``constrained``), so the answer should fit the first time; when it cannot, the model was asked
    in prose. This end is the same either way — parse, validate, repair, and raise if it still does
    not validate — because a constraint the platform did not enforce itself is a claim, not a
    guarantee: a server can ignore ``response_format``, and the repair path stays the proof.
    ``constrained`` is recorded so the failure rate can be read per decoding mode.

    It routes through :func:`generate_structured` rather than re-implementing validate-then-repair,
    which also gives that function its first caller outside its own tests — it was written for this
    path and never wired to it — so the structured-output failure rate is metered from one place.
    """
    from examlops.structured import StructuredOutputError, generate_structured

    try:
        obj = _extract_json(comp.text)
    except ValueError as exc:
        from examlops import data as _pdb

        _pdb.record_structured_output_event(
            "failed", model=comp.model, tenant=tenant, constrained=constrained
        )
        raise StructuredOutputError(f"response is not JSON: {exc}") from exc

    comp.parsed = generate_structured(
        "",  # the prompt is already spent; this call only validates + repairs what came back
        schema,
        generate_fn=lambda _prompt: obj,
        max_repairs=max_repairs,
        model=comp.model,
        tenant=tenant,
        constrained=constrained,
    )


# ── D8 guardrails at the gateway boundary (ADR 0026 clause 3) ────────────────


def default_guardrail(tenant: str = "default"):
    """The guardrail every gateway request passes through, or ``None`` when disabled.

    ADR 0026 names the gateway as the **first** boundary to scan, and until now it was the one
    boundary that made no guardrail call at all: retrieved RAG text was scanned (`rag`), the agent's
    tools were allow-listed, and a request through `exa gateway` was scanned neither on the way in
    nor on the way out.

    The default mode is **monitor**, not enforce. Monitor scans every request and records what it
    finds to `guardrail_events`, and changes nothing a caller can observe — so switching the
    boundary on cannot break traffic that was working, and an operator can see what their prompts
    actually contain before deciding to block any of it. `EXAMLOPS_GUARDRAIL_MODE=enforce` turns on
    blocking and redaction; `off` skips the scan entirely, at no cost.

    Enforce is deliberately not the default even though it is the safer-sounding value: a gateway
    that starts blocking on the day it is upgraded, on regex detectors, would be turned off wholesale
    within a day, which ends with less enforcement than monitor-then-enforce.
    """
    mode = os.getenv("EXAMLOPS_GUARDRAIL_MODE", "monitor").strip().lower()
    if mode == "off":
        return None
    try:
        from examlops.guardrails import DefaultGuardrail

        return DefaultGuardrail(
            mode=mode if mode in ("monitor", "enforce") else "monitor", tenant=tenant
        )
    except Exception:
        # A guardrail that cannot be constructed must not take the gateway down with it. The
        # boundary degrades to unscanned, which is exactly where it was before this existed.
        return None


def _guard_messages(
    guard: Any, messages: list[dict[str, Any]], tenant: str
) -> list[dict[str, Any]]:
    """Scan each message's text; return the messages, redacted where the guardrail said so.

    Per message rather than over one joined blob, because a redaction has to be written back to
    the message it came from. A message whose content is a list of parts (the OpenAI multimodal
    shape) has each ``text`` part scanned the same way. Those parts used to be passed through
    whole, which made wrapping an injected prompt in a one-element list a way round an
    enforcing guardrail. Image parts are left alone: they have their own validator
    (`MediaNotAllowed`), and a text scanner has nothing to say about an image.
    """

    def _scan(text: str) -> str:
        res = guard.check_input(text, {"tenant": tenant})
        if res.blocked:
            raise GuardrailBlocked("request", res.findings, res.reason)
        return res.text

    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            scanned = _scan(content) if content else content
            out.append({**msg, "content": scanned} if scanned != content else msg)
        elif isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                is_text = isinstance(part, dict) and part.get("type") == "text"
                text = part.get("text") if is_text else None
                if isinstance(text, str) and text:
                    scanned = _scan(text)
                    parts.append({**part, "text": scanned} if scanned != text else part)
                else:
                    parts.append(part)
            out.append({**msg, "content": parts} if parts != content else msg)
        else:
            out.append(msg)
    return out


def _cacheable(messages: list[dict[str, Any]]) -> bool:
    """Whether the semantic cache can key this request: every message is plain text.

    The cache embeds a message's text. A list of content parts — an image with a question —
    has no text to embed as a whole, and a cached answer to "what is in this chart?" must never
    be returned for a different chart.
    """
    return all(isinstance(m.get("content"), str) for m in messages)


@dataclass
class GatewayClient:
    """Thin OpenAI-compatible client with routing, failover, budget, cost + telemetry."""

    router: Router
    virtual_key: str | None = None
    tenant: str = "default"
    last_resort: Backend | None = None  # R11 degrade path
    cache_lookup: Callable[[str, list], Any] | None = None  # B3 hook
    cache_store: Callable[[str, list, Completion], None] | None = None
    #: D8 guardrail for this client. ``"auto"`` resolves from ``EXAMLOPS_GUARDRAIL_MODE`` on first
    #: use (ADR 0026 clause 3); pass an explicit ``Guardrail`` to override, or ``None`` to disable.
    guardrail: Any = "auto"

    def _guard(self):
        if self.guardrail == "auto":
            self.guardrail = default_guardrail(self.tenant)
        return self.guardrail

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        prompt_ref: str | None = None,
        response_schema: dict[str, Any] | None = None,
        max_repairs: int = 1,
        **kw: Any,
    ) -> Completion:
        """Route one chat request.

        ``prompt_ref`` names a registry prompt (``name`` or ``name@label``, ADR 0009
        clause 3). Its template is prepended as a system message, so a prompt change is a
        label move rather than a caller redeploy, and the version that served the request
        is recorded on the C1 span. A caller's own messages are never rewritten.

        ``response_schema`` (ADR 0035 clause 1) makes the response a **validated object**:
        ``Completion.parsed`` holds it, and a response that cannot be made to validate raises
        ``StructuredOutputError`` rather than returning unchecked text.
        """
        from examlops.data.finops import add_key_spend
        from examlops.data.gateway import record_gateway_call

        started = time.perf_counter()  # ADR 0023 c1: what the caller waits for, guardrail included
        key_hash = _hash_key(self.virtual_key) if self.virtual_key else None
        if self.virtual_key:
            authorize(self.virtual_key, model)  # raises typed errors before any backend call

        prompt_version: str | None = None
        template: str | None = None
        if prompt_ref:
            template, name, version = resolve_prompt_ref(prompt_ref)
            prompt_version = f"{name}@v{version}"

        # D8 inbound scan (ADR 0026 clause 3). Ahead of the cache deliberately: a blocked
        # request must not be answered from cache either, and a redaction has to reach the
        # cache key, or the redacted and unredacted forms of one prompt become two entries.
        # Only the caller's messages are scanned: the registry template is reviewed, versioned
        # text, and a template that says "you are now…" must not block every request it serves.
        guard = self._guard()
        if guard is not None:
            messages = _guard_messages(guard, messages, self.tenant)
        if template is not None:
            # Prepend, never replace: the caller's own system message still applies.
            messages = [{"role": "system", "content": template}, *messages]

        # B3 semantic-cache hook (optional; caller API unchanged, R9).
        use_cache = _cacheable(messages)
        if self.cache_lookup is not None and use_cache:
            hit = self.cache_lookup(model, messages)
            if hit is not None:
                cached = Completion(text=hit, model=model, backend="cache", cached=True)
                # The cache is keyed on the prompt, not on the schema, so an entry may have been
                # stored by a caller that asked for none. Validate it like a fresh reply; one
                # that does not fit is a miss, not an error — the model has not been asked.
                try:
                    if response_schema is not None:
                        # A cache hit is replayed text, never a constrained decode.
                        _enforce_schema(
                            cached, response_schema, tenant=self.tenant, max_repairs=max_repairs
                        )
                    return cached
                except Exception:
                    pass

        route = self.router.resolve(model)
        candidates = route.ordered() if route else []
        if self.last_resort is not None:
            candidates = candidates + [("last-resort", self.last_resort)]

        errors: list[str] = []
        for name, backend in candidates:
            # ADR 0035 clause 1: a backend that can constrain its decoder is *given* the schema,
            # so the model can only emit text that fits it. One that cannot is asked as before and
            # the answer is validated (and repaired) afterwards. Either way the caller gets a valid
            # object or a typed error — the constraint is never taken on trust.
            constrained = response_schema is not None and bool(
                getattr(backend, "constrains_schema", False)
            )
            call_kw = {**kw, "response_schema": response_schema} if constrained else kw
            try:
                raw = backend(model, messages, **call_kw)
                comp = _coerce(raw, model, name)
            except MediaNotAllowed:
                # R-V5: a policy denial on the *request* is not a backend failure —
                # retrying elsewhere would deny identically and surface the useless
                # "all backends failed". Fail fast with the real reason.
                raise
            except Exception as exc:  # failover to the next backend (R3)
                errors.append(f"{name}: {exc}")
                continue
            comp.cost_usd = comp.cost_usd or _estimate_cost(
                model, comp.prompt_tokens, comp.completion_tokens
            )
            # C1 span (best-effort) + FinOps cost (R8).
            _emit_span(model, self.tenant, comp, prompt_version=prompt_version)
            record_gateway_call(
                key_hash,
                model,
                backend=comp.backend,
                cost_usd=comp.cost_usd,
                prompt_tokens=comp.prompt_tokens,
                completion_tokens=comp.completion_tokens,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            if key_hash:
                add_key_spend(key_hash, comp.cost_usd)

            # D8 outbound scan. After the accounting on purpose: the tokens were spent and the
            # money is owed whatever the guardrail decides, so a blocked response that vanished
            # from the cost record would make the bill disagree with the provider's. Before the
            # cache on purpose too: a blocked answer must never be stored, and a redacted one
            # must be stored redacted.
            if guard is not None:
                verdict = guard.check_output(comp.text, {"tenant": self.tenant, "model": model})
                if verdict.blocked:
                    raise GuardrailBlocked("response", verdict.findings, verdict.reason)
                comp.text = verdict.text

            # A2/E2 schema enforcement (ADR 0035 clause 1). After the guardrail, so a redacted
            # response is the one validated — otherwise the object handed back could contain the
            # text the guardrail just removed. Before the cache, so only a valid object is stored.
            if response_schema is not None:
                _enforce_schema(
                    comp,
                    response_schema,
                    tenant=self.tenant,
                    max_repairs=max_repairs,
                    constrained=constrained,
                )

            if self.cache_store is not None and use_cache:
                self.cache_store(model, messages, comp)
            return comp

        # The caller got an error, so the SLI must see one: a request that failed everywhere used
        # to leave no row at all, and an error rate computed from successes alone is always zero.
        record_gateway_call(
            key_hash,
            model,
            backend=None,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            error=True,
        )
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


def _emit_span(
    model: str, tenant: str, comp: Completion, *, prompt_version: str | None = None
) -> None:
    try:
        from examlops.telemetry import genai

        with genai.genai_span("chat", system="gateway", model=model, tenant=tenant) as span:
            if prompt_version:
                # Which prompt version served this request (ADR 0009 clause 5).
                span.set_attribute("examlops.prompt.version", prompt_version)
            genai.record_usage(
                span,
                model=model,
                input_tokens=comp.prompt_tokens,
                output_tokens=comp.completion_tokens,
            )
    except Exception:
        pass


def build_default_router(*, endpoints: bool = True) -> Router:
    """The gateway's routing table: an echo route, plus every registered LLM endpoint.

    The echo route under ``EXAMLOPS_GATEWAY_DEFAULT_MODEL`` (``default``) means the table is
    never empty, so the gateway works with no model server at all. On top of it, each endpoint
    registered with `exa serve llm start` becomes a route under its own name (see
    :func:`add_endpoint_routes`). Pass ``endpoints=False`` for the echo table alone.
    """

    def _echo(model: str, messages: list[dict[str, Any]], **kw: Any) -> Completion:
        last = messages[-1]["content"] if messages else ""
        if isinstance(last, list):  # content parts: echo the text, as a text engine would
            last = " ".join(p.get("text", "") for p in last if isinstance(p, dict))
        return Completion(text=last, model=model, backend="echo", prompt_tokens=len(last.split()))

    router = Router()
    default_model = os.getenv("EXAMLOPS_GATEWAY_DEFAULT_MODEL", "default")
    router.add_route(default_model, [("echo", _echo)])
    if endpoints:
        add_endpoint_routes(router)
    return router


def add_endpoint_routes(router: Router) -> list[str]:
    """Route every addressable endpoint in the `exa serve llm` registry; return their names.

    ADR 0107 promises one request path — gateway → guardrails → engine → telemetry/FinOps — on
    every substrate. The registry and the gateway were both built and never joined: a
    registered vLLM endpoint answered `exa serve llm chat`, which talks to the server
    directly, while `exa gateway chat` could reach nothing but the echo route. So virtual
    keys, budgets, guardrails, the semantic cache and per-call cost never applied to a real
    model. Registering an endpoint now makes it a gateway route under its own name.

    Skipped: disabled or STOPPED endpoints, and endpoints with no address yet. An HPC job's
    published address is picked up here only when its endpoint file is visible on this host;
    behind the SSH transport it is fetched by `exa serve llm health` / `status` instead. An endpoint whose name matches the echo route replaces
    it — a real model beats the placeholder. There is deliberately **no** echo fallback
    behind an endpoint: answering with an echo when a real model is down would look like a
    reply. A failing endpoint surfaces as :class:`AllBackendsFailed` with its reason.

    A registry that cannot be read leaves the table as it was, so the gateway degrades to
    exactly what it did before endpoints existed.
    """
    try:
        from examlops.data.serving import list_llm_endpoints
        from examlops.llm_endpoints import resolve_address

        rows = list_llm_endpoints()
    except Exception:
        return []
    added: list[str] = []
    for rec in rows:
        name = rec.get("model")
        if not name or not rec.get("enabled", True) or rec.get("state") == "STOPPED":
            continue
        url = resolve_address(rec, fetch=False)  # local file only: no SSH per request
        if not url:
            continue
        router.add_route(str(name), [(f"endpoint:{name}", endpoint_backend(rec, url))])
        added.append(str(name))
    return added


# ── Local-engine edge (R-A1: gateway → engines.build_engine) ───────────────────
#
# The single wiring from the B2 gateway into the E2 engine layer. The dependency
# direction is strictly gateway → engines (``engines`` never imports ``gateway``),
# so a locally-hosted model (vLLM on GPU, echo on CPU/CI) becomes a first-class
# gateway backend with routing, keys, budgets, cost + telemetry applied around it.

_SAMPLING_KEYS = ("temperature", "max_tokens", "top_p", "stop", "seed")


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten OpenAI-style chat messages into a single prompt string for an engine.

    R-V4: delegates to ``engines.media.flatten_messages`` so a structured ``content`` list
    is walked part-by-part. The previous ``str(m.get("content"))`` stringified the list —
    a multimodal request arrived at the engine as a Python ``repr``.
    """
    from examlops.engines.media import flatten_messages

    prompt, _ = flatten_messages(messages)
    return prompt


def endpoint_backend(rec: dict[str, Any], base_url: str) -> Backend:
    """A gateway backend for one registered endpoint (a running ``vllm serve``).

    Built from the registry record, not the pack YAML, because the record is what the
    operator started: its engine block, its weights and its address. The engine is forced to
    the server client — the record exists *because* a server is there — so a pack YAML whose
    engine says ``echo`` or ``inproc`` cannot turn a live endpoint into a local stub.
    """
    block = dict(rec.get("engine_config") or {})
    block.update(engine="vllm-server", mode="server", base_url=base_url)
    if rec.get("served_model_name"):
        block["served_model_name"] = rec["served_model_name"]
    return engine_backend(
        str(rec.get("hf_model_id") or rec["model"]), block, label=f"endpoint:{rec['model']}"
    )


def engine_backend(model_name: str, config: Any = None, *, label: str | None = None) -> Backend:
    """R-A1: a gateway :data:`Backend` that dispatches to a local ``InferenceEngine``.

    ``config`` may be an ``engines.EngineConfig``, a plain ``dict`` (per-model YAML
    ``engine:`` block), or ``None`` (defaults). On a CPU/CI host with no vLLM the
    engine degrades to ``EchoEngine`` (R-A8), so the full gateway→engine path is
    exercisable with no GPU. The returned callable carries ``.health`` (reachable via
    :meth:`GatewayClient.health`) and ``.engine`` for introspection. ``label`` names the
    backend on each completion and in the gateway's call record (default: the engine name).
    """
    from examlops.engines import EngineConfig, build_engine, chat_via_generate, supports_chat
    from examlops.engines.media import MediaRejected

    cfg = config if isinstance(config, EngineConfig) else EngineConfig()
    if isinstance(config, dict):
        cfg = EngineConfig.from_dict(config)
    engine = build_engine(cfg, model_path=model_name)

    def _backend(model: str, messages: list[dict[str, Any]], **kw: Any) -> Completion:
        sampling = {k: v for k, v in kw.items() if k in _SAMPLING_KEYS}
        # ADR 0035 clause 1: the schema rides with the sampling knobs, and only to an engine that
        # can act on it — this filter is otherwise where a constraint silently disappears.
        if kw.get("response_schema") is not None and getattr(engine, "constrains_schema", False):
            sampling["response_schema"] = kw["response_schema"]
        try:
            if supports_chat(engine):
                # R-V4: a chat-capable engine receives the messages untouched, so
                # multimodal content parts reach vLLM intact. Media validation (R-V5)
                # happens inside the engine's own `chat`, before the HTTP call.
                ec = engine.chat(messages, **sampling)
            else:
                # Text-only engine: flatten, and warn loudly about anything dropped.
                ec = chat_via_generate(engine, messages, **sampling)
        except MediaRejected as exc:
            raise MediaNotAllowed(str(exc)) from exc
        return Completion(
            text=ec.text,
            model=model,
            backend=label or engine.name,
            prompt_tokens=ec.prompt_tokens,
            completion_tokens=ec.completion_tokens,
        )

    _backend.health = engine.health  # type: ignore[attr-defined]
    _backend.engine = engine  # type: ignore[attr-defined]
    # ADR 0035 clause 1: whether this engine can hold the model to a JSON schema while decoding.
    # Read through the telemetry wrapper, which delegates what it does not instrument.
    _backend.constrains_schema = bool(  # type: ignore[attr-defined]
        getattr(engine, "constrains_schema", False)
    )
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
