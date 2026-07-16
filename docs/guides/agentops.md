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

Thresholds are overridable per call (`loop_threshold`, `step_threshold`, `cost_budget`).

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
