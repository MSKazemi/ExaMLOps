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

## Sending agent spans to Langfuse, Phoenix or any OTLP consumer

OpenTelemetry stays the source of truth. The agent's LLM and tool calls already produce `gen_ai.*`
spans (`agent` / `tool` operations with `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `examlops.cost.usd`), and `tests/unit/test_agentops_observability.py`
asserts those attributes and the agent-to-tool parent link. Turn export on with the standard
variables; no new dependency is involved:

```bash
export OTEL_SDK_DISABLED=false
export OTEL_SERVICE_NAME=skipper
export OTEL_EXPORTER_OTLP_ENDPOINT=http://phoenix:4317     # any OTLP/gRPC receiver
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <token>"   # if the receiver needs one
```

The in-process exporter is OTLP over gRPC. Arize Phoenix accepts that directly. Langfuse ingests
OTLP over HTTP only, so route through an OpenTelemetry Collector that receives the platform's gRPC
and forwards with the `otlphttp` exporter to Langfuse's OTLP endpoint. Prompt and completion text
stays off the spans unless `EXAMLOPS_GENAI_CAPTURE_CONTENT` is set, and then goes through the
redactor. Nothing in this repository has been run against a live Langfuse or Phoenix; the claim is
limited to the attributes above.

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
| Any external service | Fully functional with local SQLite only. |

## See also

- [GenAI Observability (C1)](genai-observability.md) — the spans AgentOps consumes.
- [Guardrails (D8)](guardrails.md) — the PII redactor.
- [Evaluation (C2)](evaluation.md) / [regression gate (C3)](evaluation.md) — quality signals.
