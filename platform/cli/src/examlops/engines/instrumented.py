"""GenAI-semconv instrumentation for the LLM-serving path (ADR 0006 clause 2).

The C1 layer (:mod:`examlops.telemetry.genai`) had exactly one caller outside its own
module — the B2 gateway — so the **serving path** the decision names alongside it emitted no
GenAI span at all. Anything reaching a model other than through ``exa gateway`` (``exa models
engine``, a Ray Serve replica, a notebook holding an engine directly) was invisible.

**Why a wrapper rather than a span inside each engine.** ``build_engine`` is the one place every
engine is constructed, so instrumenting there covers the four engines that exist and every one
added later without each of them re-implementing the same three attributes slightly differently
— the drift that produced "a module only its own CLI command calls" in the first place.

**Why the span wraps the call.** The gateway's span is opened *after* its completion returns, so
its duration measures the span, not the request. These spans enclose the call, so their duration
is the model's latency — which is also what makes device-time, and therefore carbon, measurable
rather than assumed.

Everything here is a no-op when ``OTEL_SDK_DISABLED`` is truthy or unset (spec R9): the wrapper
still delegates, and :func:`examlops.telemetry.genai.genai_span` yields a no-op span.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from examlops.engines.config import Completion, supports_chat

# Which device an engine's wall-clock actually occupies, for the carbon term (clause 4).
#
# The **server** engines are deliberately absent. A ``vllm-server`` client measures the time
# its HTTP request took, but the GPU on the other end is continuously batching other requests:
# charging each client its own wall-clock would count one accelerator many times over. That
# server reports its own utilization (``VLLMServerEngine.metrics``), which is where its energy
# belongs. An engine that is not listed simply records no carbon.
#
# ``sglang`` is a server client too (ADR 0016 decision 1), so it is absent for the same reason.
_DEVICE: dict[str, str] = {"echo": "cpu", "vllm-inproc": "gpu"}


class InstrumentedEngine:
    """Wraps an :class:`~examlops.engines.config.InferenceEngine` in GenAI spans.

    Delegates every attribute it does not instrument (``config``, ``base_url``, ``models``,
    ``metrics`` …) so the wrapper is invisible to callers that reach past the contract.
    """

    def __init__(self, engine: Any, model: str) -> None:
        self._engine = engine
        self.model = model or getattr(engine, "name", "unknown")
        # A plain attribute, not a property: ``InferenceEngine`` declares ``name: str``.
        self.name = str(getattr(engine, "name", "unknown"))

    # ── delegation ────────────────────────────────────────────────────────────
    def __getattr__(self, item: str) -> Any:
        return getattr(self._engine, item)

    @property
    def engine(self) -> Any:
        """The wrapped engine, for callers that need the concrete type."""
        return self._engine

    def health(self) -> bool:
        return bool(self._engine.health())

    # ── instrumented surfaces ─────────────────────────────────────────────────
    def generate(self, prompt: str, **kw: Any) -> Completion:
        from examlops.telemetry import genai

        started = time.perf_counter()
        with genai.genai_span("model", system=self.name, model=self.model) as span:
            try:
                comp = self._engine.generate(prompt, **kw)
            except Exception as exc:
                span.record_exception(exc)
                raise
            self._record(span, time.perf_counter() - started, prompt=prompt, comp=comp)
            self._record_spec_decode(span, comp, kw)
            return comp

    def stream(self, prompt: str, **kw: Any) -> Iterator[str]:
        from examlops.telemetry import genai

        started = time.perf_counter()
        with genai.genai_span("model", system=self.name, model=self.model) as span:
            chunks = 0
            try:
                for chunk in self._engine.stream(prompt, **kw):
                    chunks += 1
                    yield chunk
            except Exception as exc:
                span.record_exception(exc)
                raise
            self._record_stream(span, time.perf_counter() - started, chunks)

    # ── recording ─────────────────────────────────────────────────────────────
    def _record(
        self, span: Any, elapsed: float, *, prompt: str, comp: Completion, op: str = "model"
    ) -> None:
        """Usage, content and carbon for one completed call. Never raises."""
        from examlops.telemetry import genai

        try:
            genai.record_usage(
                span,
                model=self.model,
                input_tokens=getattr(comp, "prompt_tokens", 0),
                output_tokens=getattr(comp, "completion_tokens", 0),
                finish_reasons=[comp.finish_reason] if getattr(comp, "finish_reason", "") else None,
            )
            genai.maybe_capture_content(span, prompt=prompt, completion=getattr(comp, "text", ""))
            self._record_carbon(span, elapsed)
        except Exception:  # noqa: BLE001 - telemetry must never break the call it measures
            pass

    def _record_spec_decode(self, span: Any, comp: Completion, kw: dict[str, Any]) -> None:
        """ADR 0016 decision 4: draft statistics → the span **and** the FinOps accumulator.

        Only a completion that proposed draft tokens is speculative; anything else is a no-op, so
        engines without speculative decoding pay one attribute read. Never raises.
        """
        try:
            proposed = int(getattr(comp, "proposed_tokens", 0) or 0)
            if proposed <= 0:
                return
            from examlops.engines import specdecode

            accepted = min(max(int(getattr(comp, "accepted_tokens", 0) or 0), 0), proposed)
            cfg = getattr(self._engine, "config", None)
            spec = getattr(cfg, "speculative_decoding", None) or {}
            lookahead = int(spec.get("num_speculative_tokens") or 1)
            acceptance = accepted / proposed
            span.set_attribute("examlops.specdecode.acceptance_rate", acceptance)
            span.set_attribute(
                "examlops.specdecode.speedup", specdecode.estimated_speedup(acceptance, lookahead)
            )
            tenant = kw.get("tenant")
            specdecode.observe(
                self.model,
                proposed_tokens=proposed,
                accepted_tokens=accepted,
                engine=self.name,
                tenant=tenant if isinstance(tenant, str) else None,
                lookahead=lookahead,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break the call it measures
            pass

    def _record_stream(self, span: Any, elapsed: float, chunks: int) -> None:
        """A stream reports what it knows: chunks and device time — never a token count.

        The engines' ``stream`` yields text fragments and no usage block, so an output-token
        figure here would be a chunk count wearing a token's name. The span's own duration is
        the latency; ``examlops.stream.chunks`` is recorded as what it is.
        """

        try:
            span.set_attribute("examlops.stream.chunks", chunks)
            self._record_carbon(span, elapsed)
        except Exception:  # noqa: BLE001
            pass

    def _record_carbon(self, span: Any, elapsed: float) -> None:
        from examlops.telemetry import genai

        device = _DEVICE.get(self.name)
        if device is None or elapsed <= 0:
            return
        hours = elapsed / 3600.0
        genai.record_carbon(
            span,
            gpu_hours=hours if device == "gpu" else 0.0,
            cpu_hours=hours if device == "cpu" else 0.0,
        )


class InstrumentedChatEngine(InstrumentedEngine):
    """:class:`InstrumentedEngine` for engines that also accept messages natively (R-V3).

    Kept a separate class rather than a conditional method because
    :func:`~examlops.engines.config.supports_chat` decides pass-through vs flattening by asking
    whether ``chat`` is callable. A wrapper that always defined ``chat`` would make every
    engine look chat-capable and silently route multimodal parts into a text-only engine — the
    exact media loss R-V4 exists to prevent.
    """

    def chat(self, messages: list[dict[str, Any]], **kw: Any) -> Completion:
        from examlops.telemetry import genai

        started = time.perf_counter()
        with genai.genai_span("chat", system=self.name, model=self.model) as span:
            try:
                comp = self._engine.chat(messages, **kw)
            except Exception as exc:
                span.record_exception(exc)
                raise
            self._record(span, time.perf_counter() - started, prompt=_flatten(messages), comp=comp)
            self._record_spec_decode(span, comp, kw)
            return comp

    def chat_stream(self, messages: list[dict[str, Any]], **kw: Any) -> Iterator[str]:
        from examlops.telemetry import genai

        started = time.perf_counter()
        with genai.genai_span("chat", system=self.name, model=self.model) as span:
            chunks = 0
            try:
                for chunk in self._engine.chat_stream(messages, **kw):
                    chunks += 1
                    yield chunk
            except Exception as exc:
                span.record_exception(exc)
                raise
            self._record_stream(span, time.perf_counter() - started, chunks)


def _flatten(messages: list[dict[str, Any]]) -> str:
    """Best-effort text of a message list, for content capture only."""
    try:
        from examlops.engines.media import flatten_messages

        return flatten_messages(messages)[0]
    except Exception:  # noqa: BLE001
        return ""


def instrument(engine: Any, model: str) -> Any:
    """Wrap ``engine`` so its calls emit GenAI spans, preserving its chat capability."""
    if isinstance(engine, InstrumentedEngine):
        return engine
    cls = InstrumentedChatEngine if supports_chat(engine) else InstrumentedEngine
    return cls(engine, model)
