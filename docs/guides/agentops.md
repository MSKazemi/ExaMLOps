# AgentOps — Agent Trace & Tool-Call Analytics (C4)

> Next-Gen 40 · feature **C4** · ADR 0021 · spec `design/vision/specs/C4-agentops.md`

AgentOps turns the Skipper agent's tool calls into **per-session analytics**: which
tools it used, how often they succeeded, how many steps a session took, how much it
cost, and whether it got stuck in a reasoning loop or blew its budget. It is
tenant-scoped (D6) and stores tool arguments **already PII-redacted** (D8), so no raw
argument ever lands in the analytics tables.

It builds on the C1 GenAI telemetry (AGENT/TOOL spans) but does not require an OTel
collector — everything degrades to the pure-SQLite `platform_db` layer.

## Concepts

| Term | Meaning |
|---|---|
| **Session** | One end-to-end agent run, keyed by `session_id`. Summarised in `agent_sessions`. |
| **Step** | One tool invocation inside a session. Stored in `agent_tool_calls`. |
| **Anomaly** | A detected problem: `loop`, `step_blowup`, `cost_overrun`, or `error_burst`. |
| **Args digest** | A PII-redacted SHA-256 (first 16 hex) of the tool args — used for loop detection without storing raw args. |

### Anomaly detection

| Code | Severity | Fires when |
|---|---|---|
| `loop` | critical | The same `(tool, redacted-args)` pair repeats ≥ 3 times. |
| `step_blowup` | critical | A session exceeds 40 steps (runaway reasoning). |
| `cost_overrun` | warn | Total session cost exceeds the budget (default $1.00). |
| `error_burst` | critical | Every step in a ≥ 3-step session failed. |

Thresholds are overridable per call (`loop_threshold`, `step_threshold`, `cost_budget`). The in-loop
breaker also emits a one-shot warning before each abort (see *Metrics, alerts and warn-before-abort*).

## CLI

```bash
# Per-tool success rate, call count, average latency (optionally one tenant)
exa agentops tools
exa agentops tools --tenant acme

# Recent sessions (newest first), filterable by status
exa agentops sessions --limit 20
exa agentops sessions --status anomaly

# Reconstruct one session's tool-call timeline (replay)
exa agentops replay sess-42

# Detect loops / step blowups / cost overruns for a session
exa agentops anomalies sess-42 --cost-budget 0.50
```

All four commands honour the global `-o/--output table|json|yaml|csv` and `--json`.

## Programmatic use

The platform records sessions through `examlops.agentops`. Instrument the agent by
collecting `AgentStep`s and flushing them at the end of a run:

```python
from examlops.agentops import AgentStep, SessionRecorder

rec = SessionRecorder("sess-42", tenant="acme", agent="skipper", model="opus")
rec.add(AgentStep("recall_memory", args={"query": "..."}, ok=True, cost_usd=0.001))
rec.add(AgentStep("trigger_retrain", args={"model": "JPCP"}, ok=True))  # audited (D4)
anomalies = rec.flush()   # persists + returns detected anomalies

if any(a.severity == "critical" for a in anomalies):
    ...  # alert (C6) or abort a runaway session
```

Or record a completed session in one call:

```python
from examlops.agentops import record_session, AgentStep, detect_anomalies, tool_success_rate

record_session("sess-42", "acme", steps)          # aggregate + persist
detect_anomalies("sess-42", cost_budget=0.5)      # re-derive from stored rows
tool_success_rate("recall_memory")                # 0.0–1.0
```

### Privacy & audit

- **PII redaction (R7/D8):** tool args are passed through `examlops.guardrails.redact_pii`
  before hashing. Two different emails collapse to the same digest, so the raw value is
  neither stored nor reconstructable.
- **Dangerous-tool audit (R7/D4):** any call to a tool in the dangerous set
  (`trigger_retrain`, `promote_model`, `approve_cluster`, `delete`, `shell`, `exec`)
  writes an `agent_dangerous_tool` event to `audit_events`.
- **Tenant scope (R3/D6):** every read and write carries a `tenant`; the CLI `--tenant`
  flag filters to one tenant.

## Data model

Two additive `platform_db` tables (no migration of existing tables):

- **`agent_sessions`** — one row per session: steps, tool_calls, errors, tokens, cost,
  status (`ok|anomaly|error`), and the detected anomaly codes.
- **`agent_tool_calls`** — one row per step: tool, redacted `args_digest`, `ok`, error,
  latency.

## Metrics, alerts and warn-before-abort

`exa slo export-metrics --out <dir>/agentops.prom` writes the platform's Prometheus series in text
format for the node_exporter textfile collector. The agent series are computed from the recorded
rows on each export, so they are correct across every process that records a session:

| Series | Type | Labels |
|---|---|---|
| `examlops_agent_sessions_started_total` | counter | `agent` |
| `examlops_agent_sessions_ended_total` | counter | `agent`, `outcome` (`ok`/`anomaly`/`error`) |
| `examlops_agent_tool_calls_total` | counter | `tool`, `outcome` (`ok`/`error`) |
| `examlops_agent_tokens_total` | counter | `agent`, `direction` (`input`/`output`) |
| `examlops_agent_cost_usd_total` | counter | `agent` |
| `examlops_agent_anomalies_total` | counter | `agent`, `code` |
| `examlops_agent_breaker_events_total` | counter | `event` (`warning`/`tripped`), `code` |
| `examlops_agent_session_duration_seconds` | histogram | `agent` (buckets 1, 5, 15, 60, 300, 900 s) |

There is deliberately no session-id, tenant or args label: those grow without bound. Counters are
totals over retained rows, so a retention prune reads as a counter reset. The export refreshes when
it is run, which is why the alerts (`AgentToolFailureRateHigh`, `AgentCircuitBreakerTripped`,
`AgentAnomalyRateHigh`, runbook [Agent runs](../runbooks/agentops.md)) use one-hour windows and a
minimum volume.

**Warn before abort.** `AgentCircuitBreaker` still aborts a runaway turn at the hard thresholds.
Before that, at `EXAMLOPS_AGENT_BREAKER_WARN_RATIO` (default 0.75) of each threshold it emits one
warning per kind per session: `loop_warning` (a call repeated twice when three aborts),
`step_blowup_warning`, `cost_warning`. Each is an `agent_breaker_warning` audit event and a
`warning` sample of the breaker metric; the abort is an `agent_breaker_tripped` event. Set the ratio
to `0` to turn warnings off; the abort is then exactly as before. Pass `on_event=` to receive
`("warning" | "tripped", Anomaly)` in process.

## Agent SLOs: error budgets and burn-rate alerts

The three alerts above are fixed rules. To hold the agent to an **objective** — with an error
budget, a burn rate, a breach audit and generated multi-window burn-rate alerts — declare a C6 SLO
([Model-quality SLOs](slos.md)) on the agent with the `c4` SLI source. The SLO's "model" is the
logical agent name recorded in `agent_sessions.agent` (`skipper` for Skipper).

```bash
# 95% of Skipper's tool calls succeed, over 30 days
exa slo set skipper tool-success --source c4 --query tool_success --target 0.95
# one tool only
exa slo set skipper status-tool --source c4 --query tool_success:platform_status --target 0.99
# 90% of sessions end clean: no loop, step blow-up or error burst
exa slo set skipper clean-sessions --source c4 --query session_ok --target 0.9

exa slo ingest skipper            # count the tool calls recorded since the last ingest
exa slo status skipper            # SLI, error budget left, burn rate
exa slo export-metrics --out /var/lib/node_exporter/examlops.prom
exa slo generate skipper tool-success --out skipper_slo_rules.yml   # burn-rate alerts
```

| Query | Good | Total |
|---|---|---|
| `tool_success` | tool calls that succeeded | tool calls |
| `tool_success:<tool>` | calls of that tool that succeeded | calls of that tool |
| `session_ok` | ended turns with status `ok` | ended turns (including turns with no tool call) |

How it counts:

- **Once per event.** Each ingest counts only events past its watermark
  (`agent_tool_calls.id` for `tool_success`, `agent_turn_outcomes.id` for `session_ok`). A session writes its tool calls before its summary row, so an ingest
  never advances past a call whose session has not been flushed yet; a call that waits more than
  10 minutes is treated as belonging to a crashed turn and no longer holds the watermark back.
- **Tenant and agent are filtered in SQL.** `--tenant` scopes the SLO to one tenant's sessions.
- **Unmeasured is not healthy.** An agent with no ended sessions ingests nothing and reports
  why; `exa slo status` shows it as unmeasured, not as a perfect SLI.
- **A breach is audited.** The ingest that spends the last of the budget writes `slo_breached`
  to `audit_events`, as for every other C6 source.
- **The alerts can fire.** For a platform-ingested source (`c1`, `c2`, `c4`, `c5`, `c8`,
  `availability`) `exa slo generate` records the SLI from `examlops_slo_sli{model,slo,tenant}` and
  takes each burn window's error ratio from the `examlops_slo_good_total` /
  `examlops_slo_events_total` counters (`increase(...[5m])` and so on) — the series
  `exa slo export-metrics` publishes. The SLI gauge is the ratio over the SLO's whole window, so
  averaging it over 5 minutes could not see a fresh burn. The ingester's own query
  (`tool_success`) is not PromQL and never reaches the rules. With a 90% target the 14.4x
  fast-burn threshold is an error ratio above 1, so that pair cannot fire; the 6x, 3x and 1x pairs
  can. Pick a target of ~93% or more if you want the fast-burn page.
- **`session_ok` counts turns.** Skipper's session id is the conversation thread, and
  `agent_sessions` keeps one row per thread whose status each turn overwrites. Every ended turn
  therefore also appends its outcome to `agent_turn_outcomes`, and `session_ok` counts that log —
  so the SLI does not depend on how often it is ingested, and an earlier turn's loop anomaly is
  not erased by a later clean turn.

## Agent spans: session, step, AGENT / TOOL / RETRIEVER / GUARDRAIL

With tracing on (`OTEL_SDK_DISABLED=false`), Skipper's LLM, tool and retriever calls are GenAI
spans (`skipper.genai_trace`), and the platform adds GUARDRAIL and RETRIEVER spans of its own.
Every span carries the kind in the vocabulary agent-observability tools group by:

| Span | `gen_ai.operation.name` | `openinference.span.kind` | Emitted by |
|---|---|---|---|
| model call | `chat` / `model` | `LLM` | Skipper, gateway, engines |
| tool call | `tool` | `TOOL` | Skipper |
| retrieval | `retrieval` | `RETRIEVER` | Skipper retrievers, `examlops.rag` |
| agent run | `agent` | `AGENT` | callers of `genai_span("agent")` |
| guardrail check | — | `GUARDRAIL` | every `DefaultGuardrail` input / output / tool check |

(Under `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` the operation names become the
registry's `chat`, `execute_tool`, `retrieval`, `invoke_agent`.)

- **Session and step.** Every Skipper span carries the session id under `gen_ai.conversation.id`
  (OTel GenAI), `session.id` (OpenInference — Phoenix and Langfuse group sessions by it) and
  `examlops.agent.session_id`. It is the turn's `thread_id`, the same value stored in
  `agent_sessions.session_id`, so a trace joins the platform's session row. It is already an
  owner hash plus the client's thread name, never a user name. `examlops.agent.step` is the
  0-based order in which the turn *started* its LLM, tool and retriever calls; parallel tool
  calls still get distinct steps.
- **Guardrail spans** record `examlops.guardrail.stage` (`input`/`output`/`tool`), `.mode`,
  `.action` (`allow`/`redact`/`block`), `.blocked` and `.findings` — the finding *categories*
  (`email`, `injection` …), never the checked text. They carry no `gen_ai.operation.name` because
  the GenAI registry defines no guardrail operation. They nest under the gateway or agent span
  that called the check. If tracing itself fails, the check runs untraced; it is never skipped.
- **Retriever spans** record the retriever's name and result count (`examlops.retrieval.documents`,
  or the RAG pipeline's `examlops.rag.doc_ids` and scores). The question is user content: it
  appears only when `EXAMLOPS_GENAI_CAPTURE_CONTENT` is set, and then only after the redactor.

## Sending agent spans to Langfuse, Phoenix or any OTLP consumer

OpenTelemetry stays the source of truth. The agent entrypoint (`agent_server.py`) installs the
platform's tracer provider at start-up (`examlops.observability.setup_tracing`). Before that
change nothing in the agent installed one, so its spans went to OpenTelemetry's no-op provider.
Two things can be configured independently:

**1. The primary exporter** (Tempo, or any OTLP receiver), with the standard variables:

```bash
export OTEL_SDK_DISABLED=false
export OTEL_SERVICE_NAME=skipper
export OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317          # gRPC (default protocol)
# or over OTLP/HTTP:
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318          # /v1/traces is appended
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer%20<token>"
```

`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `_TRACES_PROTOCOL` / `_TRACES_HEADERS` override the generic
ones, as in the OTel specification. `http/json` is not supported and falls back to gRPC with a
warning.

**2. Agent-observability consumers**, each an *additional* span processor beside the primary one:

```bash
export EXAMLOPS_OTEL_CONSUMERS=langfuse,phoenix

# Langfuse (self-hosted): OTLP/HTTP at <host>/api/public/otel/v1/traces, HTTP Basic auth
export LANGFUSE_HOST=https://langfuse.example.org
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...

# Arize Phoenix: OTLP/HTTP at <endpoint>/v1/traces, optional bearer key
export PHOENIX_COLLECTOR_ENDPOINT=https://phoenix.example.org
export PHOENIX_API_KEY=...        # optional
```

Security defaults:

- **Only listed consumers receive spans.** Langfuse keys in the environment for another reason
  never start a data flow on their own.
- **No default SaaS host.** Langfuse needs an explicit `LANGFUSE_HOST`.
- **No credential over plain HTTP** to a non-loopback host. The Langfuse keys travel as a Basic
  header and a Phoenix key as a bearer header, so such a consumer is refused and logged. For a
  development collector, `EXAMLOPS_OTEL_ALLOW_INSECURE=1` is the explicit opt-out.
- **No redirects are followed**, so a redirect cannot replay the credential to another host.
- **Bounded.** Each request times out after `OTEL_EXPORTER_OTLP_TIMEOUT` ms (default 10 000), is
  retried at most twice on 429/502/503/504, and each consumer runs in its own batch processor. A
  consumer that is down drops its own spans and costs the primary pipeline nothing.
- **A misconfigured consumer never stops the service.** It is logged and skipped.

Install `examlops[agentops]` to use the upstream `opentelemetry-exporter-otlp-proto-http`
exporter. Without it, a stdlib exporter encodes with the OTLP protobuf encoder the gRPC exporter
already depends on and posts the same gzip-compressed payload, so no new dependency is required.

What has been verified: `tests/unit/test_agentops_otlp_consumers.py` runs the real exporter
against a loopback HTTP server that decodes the OTLP protobuf the way a collector does. The server
receives the spans at Langfuse's path with its Basic auth, carrying `gen_ai.*`, `session.id` and
`openinference.span.kind`, while the primary exporter receives the same span. Nothing in this
repository has been run against a live Langfuse or Phoenix instance.

## Dashboard

The **Agent Runs** console (`/operate/agent-runs`) lists recent sessions, replays one, and shows the
per-tool success table and the breaker's warnings and aborts. It is read-only, tenant-scoped, and
shows the redacted args digest, never raw arguments (`GET /api/agentops/sessions`,
`/sessions/{id}`, `/tools`, `/breaker`).

## Graceful degradation

| Missing | Behaviour |
|---|---|
| D8 guardrails | Args are hashed without redaction (still never stored raw). |
| C1 OTel collector | Analytics still work from `platform_db`; spans are simply not emitted. |
| Langfuse / Phoenix | Listed but unreachable or misconfigured: logged and skipped; Tempo and the analytics are unaffected. |
| `examlops[agentops]` | The stdlib OTLP/HTTP exporter is used instead. |
| Any external service | Fully functional with local SQLite only. |

## See also

- [GenAI Observability (C1)](genai-observability.md) — the spans AgentOps consumes.
- [Guardrails (D8)](guardrails.md) — the PII redactor.
- [Model-quality SLOs (C6)](slos.md) — the error-budget and burn-rate machinery agent SLOs use.
- [Evaluation (C2)](evaluation.md) / [regression gate (C3)](evaluation.md) — quality signals.
