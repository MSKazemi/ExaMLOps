# Runbooks: agent runs (AgentOps)

Alerts about the Skipper agent's own behaviour: failing tools, runaway turns and flagged
sessions. The series are exported by `exa slo export-metrics --out <dir>/agentops.prom` (the
node_exporter textfile collector) from the `agent_sessions` and `agent_tool_calls` tables and the
circuit-breaker's audit events, so they refresh when that export runs. Background:
[AgentOps guide](../guides/agentops.md).

`exa agentops sessions` lists recent sessions, `exa agentops replay <session>` replays one, and
`exa agentops anomalies <session>` re-derives what was flagged.

## AgentToolFailureRateHigh {#agenttoolfailureratehigh}

**Meaning:** more than half of the last hour's calls to one tool (`tool` in the alert) failed,
with at least ten calls in that hour.

**Impact:** the agent is answering without that tool, or repeating it until the circuit-breaker
stops the turn.

**Check:** `exa agentops tools` for the failing tool's success rate, then `exa agentops replay`
on a recent failing session for the error text. A tool that needs a service (control plane, MLflow)
fails whenever that service is down.

**Fix:** restore the dependency the tool calls. If the tool itself is broken, fix or disable it in
the agent's tool pack.

## AgentCircuitBreakerTripped {#agentcircuitbreakertripped}

**Meaning:** the in-loop breaker aborted at least one turn in the last hour. `code` is the rule:
`loop` (the same call repeated), `step_blowup` (too many steps), `error_burst` (every call
failed) or `cost_overrun`.

**Impact:** the user got the breaker notice instead of an answer. A `loop_warning`,
`step_blowup_warning` or `cost_warning` seen earlier (event `warning` in
`examlops_agent_breaker_events_total`) is the same session approaching that abort.

**Check:** `exa agentops sessions --status anomaly`, then `exa agentops replay <session>`. A prompt
or tool that invites the loop is the usual cause.

**Fix:** correct the tool result or prompt that causes the repeat. Thresholds are the breaker's
constructor arguments; the soft-warning ratio is `EXAMLOPS_AGENT_BREAKER_WARN_RATIO` (default 0.75,
0 disables warnings and leaves the abort unchanged).

## AgentAnomalyRateHigh {#agentanomalyratehigh}

**Meaning:** more than a quarter of the last hour's ended sessions were flagged with a loop,
step blow-up, cost overrun or error burst, with at least five sessions in that hour.

**Impact:** the agent is unreliable right now, even if no single turn was aborted.

**Check:** `examlops_agent_anomalies_total` by `code` shows which rule dominates; the tool-failure
alert above usually fires alongside an `error_burst`.

**Fix:** as for the individual rule. A `cost_overrun` on its own is a budget question: see
`examlops_agent_cost_usd_total`.
