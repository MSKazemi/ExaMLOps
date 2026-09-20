"""C1 — OpenTelemetry GenAI semantic-convention span helpers (ADR 0006).

Emit GenAI-semconv-conformant spans from every LLM/agent invocation, reusing the
phase-17 OTLP→Tempo pipeline (:mod:`examlops.observability`). Every helper is a
**no-op when ``OTEL_SDK_DISABLED`` is truthy or unset** (spec R9) so local dev,
tests, and the non-monitoring stack are unaffected.

Design goals (spec §2):
* gen_ai.* attributes on a `model`/`agent`/`tool` span (R1/R2/R4).
* ExaMLOps extras — alias/version, tenant, request_hash — for join with eval,
  feedback, drift, and A2 lineage (R3/R8).
* Prompt/completion content captured only when ``EXAMLOPS_GENAI_CAPTURE_CONTENT``
  is truthy, and always through a redaction hook (R5/R6).
* Derived ``examlops.cost.usd`` from token usage, feeding FinOps (R7).

Which conventions are emitted (ADR 0115, ADR 0148 d2) — two sets, never both:

* **default** — the OpenTelemetry GenAI semconv ``1.27.0`` shape this instrumentation first
  pinned (``gen_ai.system``, operation names ``model``/``agent``/``tool``); see
  ``SEMCONV_VERSION``. 1.27.0 was an ordinary Development-status release: the GenAI conventions
  have not been declared stable, and every span shape in them is still Development.
* **opt-in** (``OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental``) — the current names,
  checked against the GenAI registry of ``open-telemetry/semantic-conventions-genai`` at
  ``LATEST_REVISION`` (the repository has no tagged release): ``gen_ai.provider.name`` and the
  registry's operation names (``text_completion``, ``chat``, ``invoke_agent``, ``execute_tool`` …).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import Any

SEMCONV_VERSION = "1.27.0"
# The semantic-conventions-genai commit the opt-in names were checked against (2026-09-10).
LATEST_REVISION = "0c8759497519"
_TRUTHY = {"1", "true", "yes", "on"}

# Semconv stability opt-in (ADR 0006 clause 5). The GenAI area does **not** use the
# ``<area>/dup`` dual-emit token the HTTP conventions use: OpenTelemetry defines a single
# value, ``gen_ai_latest_experimental``, which selects the latest experimental conventions
# the instrumentation supports *instead of* the version it pinned. Absent the opt-in, an
# instrumentation keeps emitting whatever it already emitted — here, ``SEMCONV_VERSION``.
LATEST_EXPERIMENTAL = "gen_ai_latest_experimental"

# gen_ai.operation.name → OTel span kind mapping for the three ExaMLOps span roles.
_SPAN_KINDS = {"model", "agent", "workflow", "tool", "chat", "embeddings", "retrieval"}

# Under the opt-in, each ExaMLOps span role emits the registry's operation name. ``model`` is a
# prompt-in/text-out engine call (``InferenceEngine.generate``), i.e. ``text_completion``.
_LATEST_OPERATION = {
    "model": "text_completion",
    "chat": "chat",
    "embeddings": "embeddings",
    "agent": "invoke_agent",
    "tool": "execute_tool",
    "workflow": "invoke_workflow",
    "retrieval": "retrieval",
}

# Fallback per-1K-token USD rates when a model isn't in the rate table. Self-hosted
# models resolve to 0.0 (cost is GPU-seconds, tracked separately by FinOps/carbon).
_DEFAULT_RATE_IN = 0.0005
_DEFAULT_RATE_OUT = 0.0015

# Minimal built-in rate table (USD per 1K tokens). Operators override via the
# finops cost provider / config; this is only a sensible default for the span attr.
_RATE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "claude-opus": (0.015, 0.075),
    "claude-sonnet": (0.003, 0.015),
    "llama3.1:8b": (0.0, 0.0),  # self-hosted → GPU-seconds, not $/token
    "nomic-embed-text": (0.0, 0.0),
}


def _identity(text: str) -> str:
    return text


# Redaction hook (D8 seam). Defaults to identity; D8 guardrails replaces it.
_redactor: Callable[[str], str] = _identity


def set_redactor(fn: Callable[[str], str]) -> None:
    """Install the content-redaction hook (wired by D8 guardrails)."""
    global _redactor
    _redactor = fn


def tracing_enabled() -> bool:
    """True only when OTEL is explicitly enabled (``OTEL_SDK_DISABLED`` falsy)."""
    return os.getenv("OTEL_SDK_DISABLED", "true").strip().lower() not in _TRUTHY


def content_capture_enabled() -> bool:
    """True only when ``EXAMLOPS_GENAI_CAPTURE_CONTENT`` is truthy (spec R5)."""
    return os.getenv("EXAMLOPS_GENAI_CAPTURE_CONTENT", "").strip().lower() in _TRUTHY


def semconv_opt_in() -> frozenset[str]:
    """Tokens listed in ``OTEL_SEMCONV_STABILITY_OPT_IN`` (comma-separated, per OTel)."""
    raw = os.getenv("OTEL_SEMCONV_STABILITY_OPT_IN", "")
    return frozenset(token.strip() for token in raw.split(",") if token.strip())


def latest_experimental_enabled() -> bool:
    """True when the operator opted into the latest experimental GenAI conventions."""
    return LATEST_EXPERIMENTAL in semconv_opt_in()


def semconv_version() -> str:
    """Which convention set this process is emitting (reported on every span).

    Under the opt-in this names the conventions repository revision the emitted names were
    checked against (``genai@<commit>``) — not a release number, because the conventions have
    none, and not a bare "latest", because what we emit is pinned and tested, not whatever is newest.
    """
    return f"genai@{LATEST_REVISION}" if latest_experimental_enabled() else SEMCONV_VERSION


def _as_messages(role: str, text: str) -> str:
    """One text message in the latest-experimental ``gen_ai.*.messages`` shape."""
    return json.dumps([{"role": role, "parts": [{"type": "text", "content": text}]}])


def has_rate(model: str) -> bool:
    """Whether the built-in table actually prices this model.

    ``estimate_cost`` answers for every model, charging anything it does not know a conservative
    default. That is right for a span attribute and wrong for a recorded metric: a locally served
    backend would acquire an invented dollar cost. Callers that must not invent one ask this first.
    """
    return model in _RATE_TABLE


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Derive USD cost from token usage (spec R7). Deterministic and pure.

    Unknown models fall back to a conservative default rate; self-hosted models in
    the table resolve to 0.0 (their cost is GPU-seconds via FinOps/carbon).
    """
    rate_in, rate_out = _RATE_TABLE.get(model, (_DEFAULT_RATE_IN, _DEFAULT_RATE_OUT))
    return round((input_tokens / 1000.0) * rate_in + (output_tokens / 1000.0) * rate_out, 6)


class _NoOpSpan:
    """Stand-in span when tracing is disabled — every method is a no-op (spec R9)."""

    def set_attribute(self, *_a: Any, **_k: Any) -> None:  # noqa: D401
        pass

    def set_status(self, *_a: Any, **_k: Any) -> None:
        pass

    def record_exception(self, *_a: Any, **_k: Any) -> None:
        pass

    def add_event(self, *_a: Any, **_k: Any) -> None:
        pass

    def end(self, *_a: Any, **_k: Any) -> None:
        """So a :func:`start_span` caller ends its span without asking whether it is real."""

    @property
    def is_noop(self) -> bool:
        return True


@contextmanager
def genai_span(
    kind: str,
    *,
    system: str,
    model: str,
    tenant: str = "default",
    request_hash: str = "",
    alias: str | None = None,
    version: str | None = None,
) -> Generator[Any, None, None]:
    """Open a GenAI span (spec R1-R4, R8-R9).

    ``kind`` is the gen_ai operation role (``model``/``agent``/``workflow``/``tool``/
    ``chat``/``embeddings``). Yields a live OTel span, or a :class:`_NoOpSpan` when
    ``OTEL_SDK_DISABLED`` (spec R9 — no export, behaviour unchanged).
    """
    kind = _normalize_kind(kind)
    if not tracing_enabled():
        yield _NoOpSpan()
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("examlops.genai", SEMCONV_VERSION)
    with tracer.start_as_current_span(_span_name(kind, model)) as span:
        _apply_base_attributes(
            span,
            kind,
            system=system,
            model=model,
            tenant=tenant,
            request_hash=request_hash,
            alias=alias,
            version=version,
        )
        yield span


def start_span(
    kind: str,
    *,
    system: str,
    model: str,
    tenant: str = "default",
    request_hash: str = "",
    alias: str | None = None,
    version: str | None = None,
) -> Any:
    """Start a GenAI span the caller must ``end()`` itself.

    For **callback-style** instrumentation, where start and finish are separate events with no
    enclosing block — a LangChain/LangGraph callback handler is the case this exists for. Such a
    handler cannot hold a context manager across two callbacks, and deliberately does not make
    the span *current*: detaching an OTel context token from a different task than the one that
    attached it corrupts the context for everything that runs after it.

    Returns a :class:`_NoOpSpan` when tracing is disabled, so a caller ends a span
    unconditionally and never branches on whether tracing is on.
    """
    kind = _normalize_kind(kind)
    if not tracing_enabled():
        return _NoOpSpan()

    from opentelemetry import trace

    tracer = trace.get_tracer("examlops.genai", SEMCONV_VERSION)
    span = tracer.start_span(_span_name(kind, model))
    _apply_base_attributes(
        span,
        kind,
        system=system,
        model=model,
        tenant=tenant,
        request_hash=request_hash,
        alias=alias,
        version=version,
    )
    return span


def _normalize_kind(kind: str) -> str:
    return kind if kind in _SPAN_KINDS else "model"


def operation_name(kind: str) -> str:
    """The ``gen_ai.operation.name`` this process emits for an ExaMLOps span role."""
    kind = _normalize_kind(kind)
    return _LATEST_OPERATION[kind] if latest_experimental_enabled() else kind


def _span_name(kind: str, model: str) -> str:
    # The current conventions name a span "{gen_ai.operation.name} {model or agent name}".
    if latest_experimental_enabled():
        return f"{operation_name(kind)} {model}"
    return f"gen_ai.{kind} {model}"


def _apply_base_attributes(
    span: Any,
    kind: str,
    *,
    system: str,
    model: str,
    tenant: str,
    request_hash: str,
    alias: str | None,
    version: str | None,
) -> None:
    """The attributes every GenAI span carries — one definition for both span shapes."""
    span.set_attribute("gen_ai.operation.name", operation_name(kind))
    if latest_experimental_enabled():
        # Renamed from gen_ai.system in the current conventions; one name or the other, not both.
        span.set_attribute("gen_ai.provider.name", system)
    else:
        span.set_attribute("gen_ai.system", system)
    span.set_attribute("gen_ai.request.model", model)
    # ExaMLOps extras (R3): join keys for eval/feedback/drift + A2 lineage.
    span.set_attribute("examlops.tenant", tenant)
    if request_hash:
        span.set_attribute("examlops.request_hash", request_hash)
    if alias:
        span.set_attribute("examlops.model.alias", alias)
    if version:
        span.set_attribute("examlops.model.version", str(version))
    span.set_attribute("examlops.semconv.version", semconv_version())


def record_usage(
    span: Any,
    *,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    finish_reasons: Sequence[str] | None = None,
) -> float:
    """Record token usage + derived cost on ``span`` (spec R1, R7).

    Returns the derived USD cost so callers can feed FinOps aggregation. On a
    no-op span this still returns the computed cost (useful to callers) but sets
    no attributes.
    """
    cost = estimate_cost(model, input_tokens, output_tokens)
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
    if finish_reasons:
        span.set_attribute("gen_ai.response.finish_reasons", list(finish_reasons))
    span.set_attribute("examlops.cost.usd", cost)
    return cost


#: Content captures dropped because the redactor raised (ADR 0148 d2), this process. A capture that
#: cannot be redacted is not emitted at all — fail closed — and the loss is counted, not silent.
_REDACTION_FAILURES = 0


def redaction_failures() -> int:
    """Captures dropped this process because redaction failed (fail-closed, ADR 0148 d2)."""
    return _REDACTION_FAILURES


def note_redaction_failure() -> None:
    """Count a capture dropped because its redactor could not be built or run."""
    global _REDACTION_FAILURES
    _REDACTION_FAILURES += 1


def maybe_capture_content(
    span: Any,
    *,
    prompt: str | None = None,
    completion: str | None = None,
    redactor: Callable[[str], str] | None = None,
) -> bool:
    """Attach prompt/completion content to ``span`` — gated + redacted (spec R5/R6).

    Returns True if content was captured. Content is captured **only** when
    ``EXAMLOPS_GENAI_CAPTURE_CONTENT`` is truthy, and always passes through the
    redaction hook (D8) first.

    Under the ``gen_ai_latest_experimental`` opt-in (clause 5) the content goes on
    ``gen_ai.input.messages`` / ``gen_ai.output.messages`` — the structured message shape the
    current conventions use — instead of the flat ``gen_ai.prompt`` / ``gen_ai.completion``
    attributes this instrumentation has emitted since it pinned 1.27.0. **One or the other,
    never both:** capture is the one place content leaves the process, and emitting the same
    redacted text twice doubles the exposure surface of the very attribute the privacy gate
    exists to bound.

    ``redactor`` overrides the process-wide hook for this call (the gateway passes its
    tenant-policy redactor). **Fail closed:** if the redactor raises, that piece of content is
    *not* attached and :func:`redaction_failures` is incremented — unredacted text never rides
    on a span because the thing meant to clean it broke.
    """
    if not content_capture_enabled():
        return False
    latest = latest_experimental_enabled()
    captured = False
    fn = redactor or _redactor

    def _clean(text: str) -> str | None:
        try:
            out = fn(text)
            return out if isinstance(out, str) else None
        except Exception:  # noqa: BLE001 - fail closed, counted
            note_redaction_failure()
            return None

    if prompt is not None:
        redacted = _clean(prompt)
        if redacted is None:
            pass
        elif latest:
            span.set_attribute("gen_ai.input.messages", _as_messages("user", redacted))
        else:
            span.set_attribute("gen_ai.prompt", redacted)
        captured = captured or redacted is not None
    if completion is not None:
        redacted = _clean(completion)
        if redacted is None:
            pass
        elif latest:
            span.set_attribute("gen_ai.output.messages", _as_messages("assistant", redacted))
        else:
            span.set_attribute("gen_ai.completion", redacted)
        captured = captured or redacted is not None
    return captured


def record_carbon(
    span: Any,
    *,
    gpu_hours: float = 0.0,
    cpu_hours: float = 0.0,
    provider: str | None = None,
) -> dict[str, Any] | None:
    """Attach Green-AI energy + carbon to a GenAI span (spec R7, ADR 0006 clause 4).

    Routes through the pluggable ``carbon`` provider registry, so a span reports the same
    methodology ``exa finops carbon`` does rather than a second hard-coded formula.

    **Device-hours are an input, never a guess.** A caller that does not own the hardware for
    the duration it measured passes nothing and gets ``None`` — no attribute is better than a
    plausible one. Charging a gateway client's wall-clock to a GPU it shares with every other
    concurrent request would produce a number that is always wrong and always publishable.

    If the resolved provider has no term for an input it was given, the reason is recorded on
    the span (``examlops.carbon.unaccounted``) rather than dropped: a missing attribute and a
    silently-halved one are indistinguishable downstream.
    """
    if gpu_hours <= 0 and cpu_hours <= 0:
        return None
    try:
        from examlops.finops.carbon import CarbonInputUnaccounted, estimate_carbon_via_provider

        try:
            estimate = estimate_carbon_via_provider(
                gpu_hours, cpu_hours=cpu_hours, provider=provider
            )
        except CarbonInputUnaccounted as exc:
            span.set_attribute("examlops.carbon.unaccounted", str(exc))
            return None
        span.set_attribute("examlops.energy.kwh", float(estimate["kwh"]))
        span.set_attribute("examlops.carbon.co2e_g", float(estimate["co2e_g"]))
        if estimate.get("provider"):
            span.set_attribute("examlops.carbon.provider", str(estimate["provider"]))
        return estimate
    except Exception:  # noqa: BLE001 - telemetry must never break the call it measures
        return None
