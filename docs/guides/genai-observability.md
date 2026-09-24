# GenAI observability (OpenTelemetry semantic conventions)

ExaMLOps emits **OpenTelemetry GenAI-semconv** spans from its LLM paths, reusing the
existing OTLP → Tempo pipeline. This gives per-request token counts, derived cost and
carbon, tool-call success/failure, and trace ids that join to eval, feedback, drift, and
lineage.

**Where instrumentation runs:** all three boundaries ADR 0006 names.

| Boundary | How | Spans |
|---|---|---|
| **B2 gateway** | `gateway/__init__.py`, every routed request | `chat` |
| **Serving path** | `engines/instrumented.py` wraps every engine `build_engine` returns, so `exa models engine`, a serving replica and a directly-held engine are all covered without knowing it | `model` / `chat` |
| **Skipper** | `skipper/genai_trace.py`, a LangChain callback handler attached to every turn | `chat` per model call, `tool` per tool call |

Skipper's spans and AgentOps are complementary, not duplicates: AgentOps records tool
*outcomes* durably in `agent_tool_calls` (feeding `tool_success_rate` and the circuit-breaker),
while these spans record *timing* to the trace backend. The callback handler is used precisely
because a `ToolMessage` carries no start time — a span opened at the AgentOps sink would report
a duration of zero, and latency is most of what a tool span is for.

Design: ADR 0006 · spec `design/vision/specs/C1-otel-genai-semconv.md`. Pinned to
OpenTelemetry GenAI semconv **1.27.0** (`genai.SEMCONV_VERSION`).

## What gets emitted

Each LLM call emits a `model` span (`chat` at the gateway and on chat-native engines)
carrying `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens` and `gen_ai.response.finish_reasons` — and no usage attribute at
all when the provider reported none, because a zero token count and an unreported one are
different facts. Every span also carries
ExaMLOps extras — `examlops.tenant`, `examlops.request_hash`, `examlops.model.alias/version`
and a derived `examlops.cost.usd` — so a span joins to the eval loop, drift baselines,
FinOps and the A2 lineage graph. The RAG retriever emits a `tool` span.

**Latency is the span's own duration.** Serving-path spans *enclose* the call, so their
duration is the model's latency. The gateway's span is opened after its completion returns,
so read gateway latency from the enclosing request span, not from the `chat` span itself.

**A stream reports `examlops.stream.chunks`, never a token count.** The engine `stream`
surfaces yield text fragments with no usage block; publishing a chunk tally under a token
attribute would be a guess wearing a standard name.

### Carbon (Green-AI, ADR 0006 clause 4)

A span from an engine that owns its hardware for the duration it measured also carries
`examlops.energy.kwh`, `examlops.carbon.co2e_g` and `examlops.carbon.provider`, computed by
the same pluggable provider `exa finops carbon` uses — one methodology, not a second
hard-coded formula.

Device-hours are an input, never a guess, so **two cases deliberately carry no carbon
attribute**: a **server-mode engine** (`vllm-server`), whose GPU is continuously batching
other clients — charging each one its own wall-clock would count one accelerator many times
over, and that server reports its own utilization — and the **gateway**, which does not own
the hardware behind a routed backend at all. A CPU-only inference *is* accounted: it burned
energy, and the Green-AI model has a CPU term for it.

### Which conventions get emitted

The GenAI conventions are still Development upstream, so this instrumentation pins
**1.27.0** and honours OpenTelemetry's opt-in rather than chasing renames. Without the opt-in
the 1.27.0 shape is emitted unchanged: `gen_ai.system`, operation names `model` / `agent` /
`tool`, and flat `gen_ai.prompt` / `gen_ai.completion` for captured content.

Setting `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` switches every GenAI span to
the current conventions at once:

| | Default (1.27.0) | Opt-in (current) |
|---|---|---|
| Provider | `gen_ai.system` | `gen_ai.provider.name` |
| Engine call (`generate`) | `gen_ai.operation.name=model` | `text_completion` |
| Chat / embeddings | `chat` / `embeddings` | `chat` / `embeddings` |
| Agent / tool / workflow | `agent` / `tool` / `workflow` | `invoke_agent` / `execute_tool` / `invoke_workflow` |
| Span name | `gen_ai.<kind> <model>` | `<operation> <model>` |
| Captured content | `gen_ai.prompt` / `gen_ai.completion` | `gen_ai.input.messages` / `gen_ai.output.messages` |

The opt-in names are checked against the GenAI attribute registry of the OpenTelemetry
`semantic-conventions-genai` repository at a pinned commit, because that repository has no tagged
release. Every span reports which set it used in `examlops.semconv.version`: `1.27.0`, or
`genai@<commit>` under the opt-in.

Note the GenAI area has **no** `<area>/dup` dual-emit token — unlike the HTTP conventions,
OTel defines one value that *replaces* the pinned set. Content is therefore emitted under
one shape or the other, never both: capture is the single place content leaves the process,
and writing the same redacted text twice would double the exposure the privacy gate bounds.

## Privacy — content is off by default

**Prompt and completion text are never captured** unless you explicitly set
`EXAMLOPS_GENAI_CAPTURE_CONTENT=true`. Even then, content passes through the D8
redaction hook (PII defense) before export. Treat enabling capture as a governed
action.

On the **gateway path** (`GatewayClient.chat`) the hook is the tenant redaction policy
(ADR 0148 decision 2), selected by `EXAMLOPS_TELEMETRY_REDACTION`:

| Mode | What the span carries |
|---|---|
| `enforce` (default) | PII and secrets replaced (`[redacted-email]`, ...) in prompt and completion. |
| `monitor` | Content unchanged; what would have been redacted is recorded to `guardrail_events` (direction `telemetry`). |
| `off` | Identity. |

**Fail closed.** If the redactor raises (or cannot be built) that content is *not* attached to the
span and `examlops.telemetry.genai.redaction_failures()` is incremented; the request itself is
still served. An unrecognised mode is treated as `enforce`. The prompt captured is what was sent
to the backend (after the request guardrail), and the answer is the one returned to the caller.
Not covered: spans emitted outside the gateway (engines, agents) still use the process-wide
`set_redactor` hook, which defaults to identity.

## Toggles

| Variable | Default | Effect |
|---|---|---|
| `OTEL_SDK_DISABLED` | `true` | Master switch. When truthy/unset, all GenAI instrumentation is a **no-op** — zero overhead, behaviour unchanged. |
| `EXAMLOPS_GENAI_CAPTURE_CONTENT` | unset | When truthy, capture (redacted) prompt/completion content on spans. |
| `EXAMLOPS_TELEMETRY_REDACTION` | `enforce` | `off`/`monitor`/`enforce` redaction of captured content on the gateway path. |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | unset | Comma-separated OTel opt-in. `gen_ai_latest_experimental` selects the structured message attributes. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://tempo:4317` | Where spans are exported. |

## CLI

```bash
exa genai check                                   # tracing on/off, capture, semconv version
exa genai cost --model gpt-4o --in 1200 --out 300 # estimate USD cost from token usage
exa --json genai check                            # machine-readable
```

## Instrumenting a call (library)

```python
from examlops.telemetry import genai

with genai.genai_span("model", system="openai", model="gpt-4o",
                      tenant="acme", request_hash=req_hash) as span:
    resp = call_the_model(...)
    genai.record_usage(span, model="gpt-4o",
                       input_tokens=resp.usage.in_, output_tokens=resp.usage.out,
                       finish_reasons=["stop"])
    genai.maybe_capture_content(span, prompt=prompt, completion=resp.text)  # gated + redacted
```

When tracing is disabled the context manager yields a no-op span, so the same code
runs unchanged in dev, tests, and the non-monitoring stack.

## Dashboards

`platform/infra/docker-compose/grafana/provisioning/dashboards/examlops_llm_gateway.json`
(`/d/examlops-llm-gateway`, added 2026-09-24, BL-114) charts the `llm-gateway` service's real
Prometheus metrics: request rate/error rate, `llm_gateway_ttft_seconds`/`llm_gateway_tpot_seconds`
p50/p95, token throughput by kind, B3 cache hit rate, circuit-breaker state and inflight/queue
depth per deployment, retries/fallbacks, and policy denials by reason — all genuine
histograms/counters/gauges (verified in the tree by
`tests/unit/test_grafana_panels_can_show_data.py`, which fails the build if a panel ever queries a
metric nothing emits). Not shown there: **tool success-rate** (no metric emits it — agentic tool
calls are outside this service's scope) and **$/request** (cost is tracked per call in
`platform_db.gateway_calls`, not yet exported as a Prometheus metric). C1 GenAI spans carry
`gen_ai.*`/`examlops.cost.usd` in Tempo separately, and `vllm serve`'s own `/metrics` exposes
`vllm:*` engine metrics (spec-decode acceptance, prefix-cache hit rate) that this dashboard does
not chart — reach those via a raw Prometheus query or `exa serve llm bench`/`status`.
