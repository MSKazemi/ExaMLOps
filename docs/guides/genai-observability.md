# GenAI observability (OpenTelemetry semantic conventions)

ExaMLOps emits **OpenTelemetry GenAI-semconv** spans from every LLM/agent
invocation, reusing the existing OTLP → Tempo pipeline. This gives per-request
token counts, derived cost, tool-call success/failure, and trace ids that join to
eval, feedback, drift, and lineage.

Design: ADR 0006 · spec `design/vision/specs/C1-otel-genai-semconv.md`. Pinned to
OpenTelemetry GenAI semconv **1.27.0** (`genai.SEMCONV_VERSION`).

## What gets emitted

Each LLM call emits a `model` span carrying `gen_ai.system`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.response.finish_reasons`, and latency. Agent runs emit `agent`/`workflow`
spans and each tool call a `tool` span with its name and success/failure. Every span
also carries ExaMLOps extras — `examlops.tenant`, `examlops.request_hash`,
`examlops.model.alias/version`, and a derived `examlops.cost.usd` — so a span joins
to the eval loop, drift baselines, FinOps, and the A2 lineage graph.

## Privacy — content is off by default

**Prompt and completion text are never captured** unless you explicitly set
`EXAMLOPS_GENAI_CAPTURE_CONTENT=true`. Even then, content passes through the D8
redaction hook (PII defense) before export. Treat enabling capture as a governed
action.

## Toggles

| Variable | Default | Effect |
|---|---|---|
| `OTEL_SDK_DISABLED` | `true` | Master switch. When truthy/unset, all GenAI instrumentation is a **no-op** — zero overhead, behaviour unchanged. |
| `EXAMLOPS_GENAI_CAPTURE_CONTENT` | unset | When truthy, capture (redacted) prompt/completion content on spans. |
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

The monitoring stack ships GenAI Grafana panels (tokens/s, $/request, TTFT, tool
success-rate) under `platform/infra/docker-compose/grafana/provisioning/dashboards/`.
