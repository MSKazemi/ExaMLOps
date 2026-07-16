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

Pinned semconv version — attribute names track OpenTelemetry GenAI semconv
``1.27.0`` (the last incubating release before stabilization); see ``SEMCONV_VERSION``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import Any

SEMCONV_VERSION = "1.27.0"
_TRUTHY = {"1", "true", "yes", "on"}

# gen_ai.operation.name → OTel span kind mapping for the three ExaMLOps span roles.
_SPAN_KINDS = {"model", "agent", "workflow", "tool", "chat", "embeddings"}

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
    if kind not in _SPAN_KINDS:
        kind = "model"
    if not tracing_enabled():
        yield _NoOpSpan()
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("examlops.genai", SEMCONV_VERSION)
    with tracer.start_as_current_span(f"gen_ai.{kind} {model}") as span:
        span.set_attribute("gen_ai.operation.name", kind)
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
        span.set_attribute("examlops.semconv.version", SEMCONV_VERSION)
        yield span


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


def maybe_capture_content(
    span: Any, *, prompt: str | None = None, completion: str | None = None
) -> bool:
    """Attach prompt/completion content to ``span`` — gated + redacted (spec R5/R6).

    Returns True if content was captured. Content is captured **only** when
    ``EXAMLOPS_GENAI_CAPTURE_CONTENT`` is truthy, and always passes through the
    redaction hook (D8) first.
    """
    if not content_capture_enabled():
        return False
    captured = False
    if prompt is not None:
        span.set_attribute("gen_ai.prompt", _redactor(prompt))
        captured = True
    if completion is not None:
        span.set_attribute("gen_ai.completion", _redactor(completion))
        captured = True
    return captured
