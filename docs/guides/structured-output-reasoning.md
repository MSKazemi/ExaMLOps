# Structured Output & Reasoning Ops (B8)

> Next-Gen 40 · feature **B8** · ADR 0035 · spec `design/vision/specs/B8-structured-output-reasoning-ops.md`

B8 gives the gateway (B2) / engine (E2) two capabilities that everything downstream —
Skipper tool calls, RAG citations, extraction — depends on:

1. **Structured output** that is *guaranteed valid* against a schema.
2. **Reasoning ops**: enforce a thinking budget, account reasoning vs output cost
   separately, and capture redacted, TTL'd, tenant-scoped reasoning traces.

Everything is pure Python (uses `jsonschema` when installed, degrades to a built-in
validator) — no LLM, engine, or provider API needed to validate, repair, budget, or account.

## Structured output — validate → repair → else fail

```python
from examlops.structured import generate_structured

schema = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"],
}
obj = generate_structured(prompt, schema, generate_fn=call_model, model="llama3.1-8b")
# obj is guaranteed to validate against `schema`
```

The rare invalid output is **repaired** — scalar types coerced (`"5"` → `5`), unknown
properties dropped, required-but-missing filled with a typed zero — and revalidated. Only if
it still fails does it raise `StructuredOutputError` (R2). Agent tool-call args and RAG
citations use this so their structure is never "hopefully valid" (R3).

Every attempt is metered as `valid` / `repaired` / `failed`, giving a structured-output
failure rate (R8), together with whether the decoder was constrained (below).

## Constrained decoding — the model cannot produce a wrong shape

Repair reaches a valid object by rewriting a wrong answer. A **constraint** means the wrong answer
was never possible: the server compiles the schema into a grammar and can only emit text that fits
it. `GatewayClient.chat(..., response_schema=…)` uses it whenever the backend can:

| Backend | What it is sent |
|---|---|
| `vllm-server` — a running `vllm serve`, or any OpenAI-compatible endpoint | `response_format: {"type": "json_schema", "json_schema": {"name": "response", "schema": …, "strict": true}}` |
| `vllm-inproc` — vLLM's offline API | `SamplingParams(guided_decoding=GuidedDecodingParams(json=…))` |
| anything else (`echo`, a plain callable, SGLang) | nothing — it is asked as before |

An engine declares the capability with `constrains_schema = True`, and `engine_backend` carries
that to the gateway, so a backend is handed a schema only when it can act on it.

**The constraint is never taken on trust.** Validation and repair run afterwards either way: a
server can ignore `response_format`, and a caller's guarantee must not rest on a field the
platform did not enforce itself. A constrained answer that still needs repair is therefore
visible rather than hidden — `structured_output_stats()` counts `constrained` alongside
`valid` / `repaired` / `failed`, so a backend that claims the capability and does not honour it
shows up as repairs on constrained calls.

Two deliberate limits: a **cache hit** is replayed text, never a constrained decode, so it is
counted as unconstrained; and the schema travels as one field — it never leaks into the sampling
knobs sent to the server.

### `exa gateway schema test`

```bash
exa gateway schema test schema.json candidate.json
# Object is valid against the schema.
# ...or, on a fixable object:
# Invalid — repaired to a valid object:
#   { "name": "x", "age": 5 }
```

## Reasoning ops

### Budget (thinking cut off at the limit)

```bash
exa gateway reasoning budget 5000 --max 1000
# Thinking cut off at 1000 tokens (requested 5000).
```

```python
from examlops.structured import ReasoningBudget
allowed, was_cut = ReasoningBudget(max_thinking_tokens=1000).enforce(5000)   # (1000, True)
```

### Separate reasoning-vs-output accounting

Reasoning ("thinking") tokens are often billed differently from output tokens; B8 accounts
them **separately** for C1 telemetry and FinOps:

```bash
exa gateway reasoning account llama3.1-8b --reasoning 1000 --output 200 \
    --reasoning-rate 0.001 --output-rate 0.002
# llama3.1-8b: reasoning 1000tok ($1.0) + output 200tok ($0.4) = $1.4

exa gateway reasoning stats --model llama3.1-8b
```

### Redacted, TTL'd, tenant-scoped traces

Where a reasoning trace is exposed, capturing it for observability (C4) must not leak PII or
retain it forever:

```python
from examlops.structured import capture_reasoning_trace, get_reasoning_trace

capture_reasoning_trace("req-42", trace_text, tenant="acme", ttl_seconds=3600)
get_reasoning_trace("req-42")   # redacted trace, or None once the TTL passes
```

The trace is **redacted** through the D8 guardrail PII redactor before storage, stamped with
the tenant, and expires after its TTL (R6 / GWT-6).

## Related

- **B2 / E2** — the gateway + engine that perform constrained decoding in production.
- **C1** telemetry / **FinOps** — the reasoning-vs-output cost split feeds both.
- **C4** AgentOps — reasoning traces are captured here for session analysis.
- **D8** guardrails — the PII redactor applied to traces.
