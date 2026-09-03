"""GenAI-semconv spans for Skipper's own LLM and tool calls (ADR 0006 clause 2).

Clause 2 names three boundaries — the B2 gateway, the LLM-serving path, and **Skipper's LLM
and tool calls**. The first two are instrumented in the platform package; this is the third,
and it was the ADR's last open clause.

**Why a LangChain callback handler rather than the AgentOps seam.** ``skipper.instrument``
already sees every tool result, so it looks like the natural hook — but it sees only the
*result*. A ``ToolMessage`` carries no start time, so a span opened and closed there would
report a duration of zero, and latency is most of why a tool span is worth having.
``BaseCallbackHandler`` gets ``on_tool_start`` and ``on_tool_end`` as separate events with
real time between them, and the same for the model. The two are complementary and stay
separate: AgentOps records tool *outcomes* durably in ``agent_tool_calls`` for the success-rate
metric and the circuit-breaker; this records *timing* to a trace backend.

**Why spans are started, not entered.** Start and end arrive as two callbacks, so there is no
block to hold a context manager across, and LangGraph may run them on different asyncio tasks.
:func:`examlops.telemetry.genai.start_span` returns a span that is not made *current* for
exactly this reason: detaching an OTel context token from a task other than the one that
attached it corrupts the context for everything after it.

Fail-open throughout: if the platform CLI is not importable (the agent can run without it) or
a callback raises, the turn proceeds untouched. A callback handler that can break a chat turn
is worse than no telemetry.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - import guard (langchain is a hard dep of the agent, not of tests)
    from langchain_core.callbacks import BaseCallbackHandler

    _BASE: Any = BaseCallbackHandler
except Exception:  # noqa: BLE001
    _BASE = object


def _genai() -> Any:
    """The C1 telemetry module, or ``None`` when the platform CLI is not on the path."""
    try:
        from examlops.telemetry import genai

        return genai
    except Exception:  # noqa: BLE001
        return None


def _model_name(serialized: dict | None, metadata: dict | None) -> str:
    """Best available model id for the span, from whatever LangChain passed."""
    for source, key in (
        (metadata, "ls_model_name"),
        (serialized, "name"),
    ):
        value = (source or {}).get(key)
        if value:
            return str(value)
    kwargs = (serialized or {}).get("kwargs") or {}
    for key in ("model", "model_name", "model_id"):
        if kwargs.get(key):
            return str(kwargs[key])
    return "unknown"


def _usage(response: Any) -> tuple[int, int, list[str]]:
    """Token usage + finish reasons from an ``LLMResult``, across provider shapes.

    Providers report usage in more than one place — ``usage_metadata`` on the message
    (LangChain's normalized form) and ``llm_output['token_usage']`` (the provider's own). The
    normalized form is preferred; the raw one is the fallback. When neither is present the
    counts stay 0 and **no usage attribute is set** by the caller, because a zero token count
    and an unreported one are different facts.
    """
    generations = getattr(response, "generations", None) or []
    finish: list[str] = []
    for batch in generations:
        for gen in batch or []:
            info = getattr(gen, "generation_info", None) or {}
            reason = info.get("finish_reason") or info.get("done_reason")
            if reason:
                finish.append(str(reason))
            message = getattr(gen, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if usage:
                return (
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                    finish,
                )
    raw = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
    return (
        int(raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0),
        int(raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0),
        finish,
    )


class SkipperTracer(_BASE):  # type: ignore[misc,valid-type]
    """Opens a GenAI span per LLM call and per tool call for one process.

    One handler instance serves every turn; spans are keyed by LangChain's ``run_id``, which is
    unique per run and is what pairs a start callback with its end.
    """

    def __init__(self, *, tenant: str = "default", system: str = "skipper") -> None:
        self.tenant = tenant
        self.system = system
        # run_id → (span, model). The model is carried here rather than stashed on the span:
        # an SDK Span defines __slots__, so an attribute set on it would raise.
        self._spans: dict[Any, tuple[Any, str]] = {}

    # ── model ────────────────────────────────────────────────────────────────
    def on_chat_model_start(
        self,
        serialized: dict | None,
        messages: Any,
        *,
        run_id: Any = None,
        metadata: dict | None = None,
        **_kw: Any,
    ) -> None:
        self._start("chat", run_id, _model_name(serialized, metadata))

    def on_llm_start(
        self,
        serialized: dict | None,
        prompts: Any,
        *,
        run_id: Any = None,
        metadata: dict | None = None,
        **_kw: Any,
    ) -> None:
        self._start("model", run_id, _model_name(serialized, metadata))

    def on_llm_end(self, response: Any, *, run_id: Any = None, **_kw: Any) -> None:
        entry = self._spans.pop(run_id, None)
        if entry is None:
            return
        span, model = entry
        genai = _genai()
        try:
            if genai is not None:
                inp, out, finish = _usage(response)
                if inp or out:
                    genai.record_usage(
                        span,
                        model=model,
                        input_tokens=inp,
                        output_tokens=out,
                        finish_reasons=finish or None,
                    )
                elif finish:
                    span.set_attribute("gen_ai.response.finish_reasons", finish)
        except Exception:  # noqa: BLE001 - telemetry must never break the turn
            pass
        finally:
            self._end(span)

    def on_llm_error(self, error: BaseException, *, run_id: Any = None, **_kw: Any) -> None:
        self._fail(run_id, error)

    # ── tools ────────────────────────────────────────────────────────────────
    def on_tool_start(
        self,
        serialized: dict | None,
        input_str: str,
        *,
        run_id: Any = None,
        **_kw: Any,
    ) -> None:
        self._start("tool", run_id, str((serialized or {}).get("name") or "tool"))

    def on_tool_end(self, output: Any, *, run_id: Any = None, **_kw: Any) -> None:
        entry = self._spans.pop(run_id, None)
        if entry is None:
            return
        span = entry[0]
        # A LangChain ``ToolMessage`` with status="error" is delivered through *on_tool_end*,
        # not on_tool_error — a tool that returns its failure rather than raising would
        # otherwise be recorded as a success with a plausible latency.
        try:
            if getattr(output, "status", None) == "error":
                span.set_attribute("error.type", "tool_error")
        except Exception:  # noqa: BLE001
            pass
        self._end(span)

    def on_tool_error(self, error: BaseException, *, run_id: Any = None, **_kw: Any) -> None:
        self._fail(run_id, error)

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _start(self, kind: str, run_id: Any, model: str) -> None:
        genai = _genai()
        if genai is None or run_id is None:
            return
        try:
            span = genai.start_span(kind, system=self.system, model=model, tenant=self.tenant)
            self._spans[run_id] = (span, model)
        except Exception:  # noqa: BLE001
            pass

    def _end(self, span: Any) -> None:
        try:
            span.end()
        except Exception:  # noqa: BLE001
            pass

    def _fail(self, run_id: Any, error: BaseException) -> None:
        entry = self._spans.pop(run_id, None)
        if entry is None:
            return
        span = entry[0]
        try:
            span.record_exception(error)
            span.set_attribute("error.type", type(error).__name__)
        except Exception:  # noqa: BLE001
            pass
        self._end(span)


def tracer(tenant: str = "default") -> SkipperTracer | None:
    """A handler, or ``None`` when tracing is off — so callers attach nothing by default."""
    genai = _genai()
    if genai is None:
        return None
    try:
        if not genai.tracing_enabled():
            return None
    except Exception:  # noqa: BLE001
        return None
    return SkipperTracer(tenant=tenant)


def traced(cfg: dict, tenant: str = "default") -> dict:
    """Return ``cfg`` with the GenAI callback attached — the one way a turn gets instrumented.

    Every ``graph.stream`` call site goes through this; a guard test enforces it, because a
    second call site that forgets is exactly how clause 2 came to be half-implemented in the
    first place. When tracing is disabled this returns ``cfg`` unchanged — not a config with an
    inert handler in it — so the default path is byte-identical to what it was.
    """
    handler = tracer(tenant)
    if handler is None:
        return cfg
    merged = dict(cfg)
    merged["callbacks"] = [*list(merged.get("callbacks") or []), handler]
    return merged
