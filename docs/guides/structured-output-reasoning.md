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

### Gateway enforcement (ADR 0035 clause 2)

`GatewayClient.chat` resolves a reasoning budget for every request and enforces it. Nothing
changes for a request with no budget.

**Where a budget comes from** - the *tightest* applicable one wins, so a narrow scope can tighten
a broad one but never loosen it:

| Scope | Set with |
|---|---|
| the call | `chat(..., reasoning_budget=N)` |
| a virtual key | `exa gateway reasoning set-budget N --key-hash H` |
| a project | `exa gateway reasoning set-budget N --project P` (applies to that project's keys) |
| a model | `exa gateway reasoning set-budget N --model M` |
| everything | `EXAMLOPS_REASONING_BUDGET_DEFAULT=N` |

`exa gateway reasoning budgets` lists them; `--events` shows what the gateway observed
(`within` / `exceeded` / `unknown` / `refused`).

**The cap on the wire.** A thinking cap is sent to a backend only if its engine block declares the
request field that server accepts, e.g. `reasoning_cap_param: thinking_token_budget`. Unset (the
default) sends nothing: the platform does not guess a provider's parameter name. Either way the
response is judged afterwards from the usage the server reports
(`usage.completion_tokens_details.reasoning_tokens`).

**Over budget.** `EXAMLOPS_REASONING_BUDGET_MODE`: `enforce` (default) withholds the response with
`ReasoningBudgetExceeded`; `strict` also refuses a response whose backend reported no reasoning
usage; `flag` serves it and records the breach; `off` disables the gate. The tokens were already
spent, so the call stays in the cost ledger and the split accounting; the response is not cached,
and an `audit_events` row (`reasoning_budget_exceeded`) is written.

**Unknown is not zero.** A backend that reports no reasoning usage yields `reasoning_status =
"unknown"`, recorded as its own outcome - it never passes as "0 thinking tokens".

**Accounting.** Whenever a backend reports reasoning tokens, `reasoning_usage` records the split
(reasoning vs. the rest of `completion_tokens`, both at the output rate, since providers bill
thinking as output), and the GenAI span carries `examlops.usage.reasoning_tokens`.

**Not covered:** `GatewayClient` has no streaming path, so there is nothing to bypass the gate; a
streaming path added later must route through it (a test pins this).

### Redacted, TTL'd, tenant-scoped traces

Reasoning traces are content. `capture_reasoning_trace` redacts through the tenant telemetry
redactor (`guardrails.telemetry_redactor`, ADR 0148 d2), stamps the tenant and a TTL, and **fails
closed**: if redaction fails nothing is stored. The gateway captures a backend-returned trace only
when `EXAMLOPS_REASONING_TRACE_CAPTURE=1` (TTL `EXAMLOPS_REASONING_TRACE_TTL_SECONDS`, default
86400), and never returns the raw trace to the caller.

```python
from examlops.structured import capture_reasoning_trace, get_reasoning_trace

capture_reasoning_trace("req-42", trace_text, tenant="acme", ttl_seconds=3600)
get_reasoning_trace("req-42")   # redacted trace, or None once the TTL passes
```

## Policy — default schemas, route defaults, budget gates (ADR 0035 clause 3)

### Default schemas for platform outputs

The platform ships the output shapes it produces itself, so callers name them instead of
re-typing a JSON Schema:

| Name | Shape |
|---|---|
| `tool_call` | `{"name": str, "arguments": object}` — one agent tool invocation |
| `rag_answer` | `{"answer": str, "citations": [int]}` — chunk numbers the answer relies on |
| `extraction` | `{"fields": object, "confidence"?: 0..1}` |
| `classification` | `{"label": str, "confidence"?: 0..1, "rationale"?: str}` |

```python
comp = client.chat("qwen3", messages, response_schema="tool_call")
comp.parsed   # {"name": ..., "arguments": {...}} — validated, extras repaired away
```

An unknown name raises `UnknownSchemaError` **before** any backend is called. A caller's own
dict schema is still accepted and always wins.

```bash
exa gateway schema list               # built-in + site schemas, and route defaults
exa gateway schema show rag_answer    # print one
exa gateway schema test classification out.json   # a registered name works as SCHEMA_FILE
```

### `structured.yaml` — site schemas and per-route defaults

`<config dir>/structured.yaml` (override with `EXAMLOPS_STRUCTURED_CONFIG`; the config dir is
`EXAMLOPS_CONFIG_DIR` → `<EXAMLOPS_DATA_DIR>/config` → `~/.config/examlops`):

```yaml
version: 1
schemas:
  ticket:                       # a site schema; may not reuse a built-in name
    type: object
    properties: {severity: {type: integer}, summary: {type: string}}
    required: [severity, summary]
routes:                         # gateway route (logical model) or fnmatch glob
  "support-*":
    response_schema: ticket     # applied when the caller names no schema
    reasoning_budget: 2000      # one more budget candidate
  support-eu:
    reasoning_budget: 500
```

- A route's **schema** comes from its exact entry first, then globs in file order.
- A route's **budget** is the tightest of every matching entry, and it joins the request / key /
  project / model / `EXAMLOPS_REASONING_BUDGET_DEFAULT` candidates — the tightest overall wins, so
  a route default tightens but never lifts a cap. Its events and audit rows carry `source=route`.
- The file is validated **totally** (every problem, with its key path — `exa gateway schema list`
  prints them and exits 1). At request time an invalid file is ignored **as a whole** and a warning
  is logged; the half that parsed is never applied. That includes its route **budgets**: while the
  file is invalid no route cap applies, so run `exa gateway schema list` after every edit, and
  back a cap you cannot afford to lose with a key/project/model budget or a D5 `not has_budget`
  rule (below), which do not depend on this file.
- With a route default schema, a plain `exa gateway chat` on that route is structured too: an
  answer that cannot be made valid exits 1 (`StructuredOutputError`), and `-o json` carries the
  validated object as `parsed`.
- The file is re-read when its mtime or size changes; no restart is needed.

### Budgets gated via D5 (policy-as-code)

Before any backend is called, the gateway consults `policy.yaml` for the action
`reasoning_request` with the resolved budget in the context — `model`/`route`, `tenant`,
`project`, `key_hash`, `has_budget`, `budget_tokens` (`-1` when none), `budget_source`:

```yaml
policies:
  - action: reasoning_request
    when: "not has_budget"
    effect: deny                # no unbounded thinking
  - action: reasoning_request
    when: "budget_tokens > 8000"
    effect: require_approval    # there is no human inside a request: this refuses too
```

Anything but `allow` raises `ReasoningPolicyDenied` and nothing is spent. A matched rule is
audited (`policy:reasoning_request`); with no matching rule no per-request audit row is written.
A policy engine that **raises** denies (`decide_safe`). `when:` conditions are evaluated with
`simpleeval`, a core dependency of `examlops`; a condition that cannot be evaluated does not
match, and a later catch-all rule decides.

A refusal propagates to library callers as-is — an unstructured `RagPipeline.query` raises
`ReasoningPolicyDenied` rather than returning its offline placeholder answer.

Changing a budget is governed too: `exa gateway reasoning set-budget` (and `--remove`) consults
the `reasoning_budget_set` action (context `scope`, `ref`, `tenant`, `max_thinking_tokens` —
`-1` for a removal, `op`), refuses with exit 1 on anything but `allow`, and writes a `reasoning_budget_set` /
`reasoning_budget_removed` audit event.

### Structured RAG answers

`RagPipeline.query(..., structured=True)` asks for `rag_answer` (through the gateway with
`response_schema="rag_answer"`, or validating a custom `generate_fn`'s JSON), and returns only
the chunks the model **cited** as `citations`. A cited number outside the retrieved context, or
a citation that is not an integer at all (possible when `jsonschema` is not installed and the
fallback validator does not check array items), is dropped and reported in `dropped_citations`; an answer that cannot be made valid raises
`StructuredOutputError`. The default (`structured=False`) is unchanged.

### Not covered yet

- **The HTTP gateway service** (`examlops.gateway.service`, ADR 0155) applies neither reasoning
  budgets nor schemas; both are enforced by `GatewayClient` (in-process callers, `exa gateway
  chat`, RAG).
- **Skipper tool calls** are validated by the agent framework, not by `tool_call`.
- **C6 SLO gating** of reasoning budgets is not wired.

## Related

- **B2 / E2** — the gateway + engine that perform constrained decoding in production.
- **C1** telemetry / **FinOps** — the reasoning-vs-output cost split feeds both.
- **C4** AgentOps — reasoning traces are captured here for session analysis.
- **D8** guardrails — the telemetry redactor applied to traces.
