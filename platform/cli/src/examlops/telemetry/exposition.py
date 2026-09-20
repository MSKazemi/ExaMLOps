"""Prometheus text-format exposition of metrics the platform records itself.

**The gap this closes spans three ADRs.** The platform ingests SLIs into `slo_samples` (ADR 0023
clause 3) and records vector build/query latency into `vector_metrics` (ADR 0020 clause 5), and
neither is visible to Prometheus. That has a consequence beyond dashboards: `exa slo generate`
emits **burn-rate alert rules that range over a Prometheus series**, so for any SLI the platform
ingests itself — the `c2` eval, `c5` drift and `c8` fairness sources — the alert can never fire.
ADR 0025's clause 3 asks for "a C6 fairness SLI **and alert**"; the SLI landed in iteration 18 and
the alert had nowhere to fire from.

**Why a textfile rather than an HTTP endpoint.** Prometheus' node_exporter textfile collector is
the standard path for metrics produced by batch jobs and CLIs, and it needs no long-lived process:
`exa` writes a `.prom` file, node_exporter serves it. The control plane's own `/metrics` reads a
*different* database (its approval store) and is the wrong owner for platform state. Adding a
second long-lived service to publish rows a CLI can write is cost without a reason.

Everything here is a pure formatter over already-recorded rows: no scraping, no timers, no state.
"""

from __future__ import annotations

from typing import Any

#: Written before each metric family. Prometheus tolerates their absence; a reader does not.
_HELP: dict[str, tuple[str, str]] = {
    "examlops_slo_sli": ("gauge", "Current SLI ratio (good/total) for a declared SLO"),
    "examlops_slo_target": ("gauge", "Objective ratio the SLO is held to"),
    "examlops_slo_budget_remaining": (
        "gauge",
        "Fraction of the error budget still available (1 = untouched, <0 = exhausted)",
    ),
    "examlops_slo_burn_rate": ("gauge", "Observed error over allowed error"),
    "examlops_slo_samples": ("gauge", "Samples the SLI was computed from"),
    "examlops_slo_measured": (
        "gauge",
        "1 when the SLO has samples, 0 when it has none — an unmeasured SLO is not a healthy one",
    ),
    "examlops_vector_latency_ms": (
        "gauge",
        "Most recent latency of a vector-store operation, in milliseconds",
    ),
    "examlops_vector_items": ("gauge", "Items touched by the most recent vector-store operation"),
    # AgentOps (ADR 0021 decision 3). Every label is a bounded set: agent, status, tool, code.
    "examlops_agent_sessions_started_total": ("counter", "Agent sessions recorded, by agent"),
    "examlops_agent_sessions_ended_total": (
        "counter",
        "Agent sessions that ended, by agent and outcome (ok | anomaly | error)",
    ),
    "examlops_agent_steps_total": (
        "counter",
        "Tool-call steps taken by agent sessions, by agent (divide by sessions for average steps)",
    ),
    "examlops_agent_tool_calls_total": (
        "counter",
        "Agent tool calls by tool and outcome (ok | error)",
    ),
    "examlops_agent_tokens_total": ("counter", "Agent LLM tokens by agent and direction"),
    "examlops_agent_cost_usd_total": ("counter", "Agent session cost in USD, by agent"),
    "examlops_agent_anomalies_total": (
        "counter",
        "Sessions in which a loop, step blow-up, cost overrun or error burst was detected",
    ),
    "examlops_agent_breaker_events_total": (
        "counter",
        "In-loop circuit-breaker events by event (warning | tripped) and anomaly code",
    ),
    "examlops_agent_session_duration_seconds": (
        "histogram",
        "Wall-clock duration of ended agent sessions",
    ),
}

#: Suffixes a histogram's samples carry; HELP/TYPE belong to the family name without them.
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def _family(metric: str) -> str:
    """The family a sample belongs to: ``x_bucket`` -> ``x`` when ``x`` is a histogram."""
    for suffix in _HISTOGRAM_SUFFIXES:
        base = metric[: -len(suffix)]
        if metric.endswith(suffix) and _HELP.get(base, ("",))[0] == "histogram":
            return base
    return metric


def _escape(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: dict[str, Any]) -> str:
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(pairs.items()) if v is not None)
    return f"{{{inner}}}" if inner else ""


def render(samples: list[tuple[str, dict[str, Any], float]]) -> str:
    """Render ``(metric, labels, value)`` triples as Prometheus text format.

    `# HELP`/`# TYPE` are emitted **once per family**, before its first sample, which is what the
    format requires — repeating them per sample makes the file unparseable rather than merely
    verbose.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for metric, labels, value in samples:
        family = _family(metric)
        if family not in seen:
            seen.add(family)
            kind, help_text = _HELP.get(family, ("gauge", family))
            lines.append(f"# HELP {family} {help_text}")
            lines.append(f"# TYPE {family} {kind}")
        lines.append(f"{metric}{_labels(labels)} {float(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def slo_samples(model: str | None = None, tenant: str = "default") -> list[tuple]:
    """SLO gauges for every declared SLO, or for one model.

    **An unmeasured SLO exports `measured=0` and no SLI.** Publishing its placeholder 1.0 would
    put a perfect ratio on a dashboard for something nobody has measured, and a burn-rate alert
    cannot fire on a perfect ratio — the same trap the control plane's `/metrics` was fixed for.
    """
    from examlops.data.governance import list_slo_specs
    from examlops.slo import slo_status

    models = [model] if model else sorted({str(s["model"]) for s in list_slo_specs(tenant=tenant)})
    out: list[tuple] = []
    for name in models:
        for status in slo_status(name, tenant=tenant):
            labels = {"model": status.model, "slo": status.name, "tenant": status.tenant}
            out.append(("examlops_slo_target", labels, status.target))
            out.append(("examlops_slo_measured", labels, 1.0 if status.measured else 0.0))
            out.append(("examlops_slo_samples", labels, float(status.n)))
            if status.measured:
                out.append(("examlops_slo_sli", labels, status.sli))
                out.append(("examlops_slo_budget_remaining", labels, status.budget_remaining))
                out.append(("examlops_slo_burn_rate", labels, status.burn_rate))
    return out


def vector_samples(tenant: str | None = None) -> list[tuple]:
    """Latest latency/item-count per (collection, operation) — ADR 0020 clause 5.

    The **latest** row per series, not an average: `vector_metrics` is an append-only log, and a
    gauge that averaged a collection's whole history would move less and less as the log grew,
    which is the opposite of what an operator watching a reindex needs.
    """
    from examlops.data.data_assets import latest_vector_metrics

    out: list[tuple] = []
    for row in latest_vector_metrics(tenant):
        labels = {
            "collection": row["collection"],
            "tenant": row["tenant"],
            "operation": row["operation"],
        }
        out.append(("examlops_vector_latency_ms", labels, float(row["latency_ms"])))
        out.append(("examlops_vector_items", labels, float(row["item_count"])))
    return out


def agent_samples() -> list[tuple]:
    """AgentOps series from the recorded sessions, tool calls and breaker events (ADR 0021 d3).

    Derived from ``platform.db`` on every export, like the SLO gauges, so it is correct across every
    process that records a session (the agent service, the CLI, the autopilot) and survives a
    restart. **Cardinality discipline:** the only labels are ``agent``, ``status``, ``tool``,
    ``code``, ``event``, ``direction`` and ``le`` — never a session id, tenant or args digest.
    Counters are totals over the retained rows; a retention prune can lower one, which Prometheus
    ``rate()``/``increase()`` reads as a counter reset.
    """
    from examlops.data.agent import DURATION_BUCKETS_S, agent_metrics_rollup

    roll = agent_metrics_rollup()
    out: list[tuple] = []
    started: dict[str, float] = {}
    tokens: dict[tuple[str, str], float] = {}
    cost: dict[str, float] = {}
    steps: dict[str, float] = {}
    for r in roll["sessions"]:
        agent = r["agent"]
        steps[agent] = steps.get(agent, 0.0) + float(r["steps"])
        started[agent] = started.get(agent, 0.0) + float(r["started"])
        if r["ended"]:
            out.append(
                (
                    "examlops_agent_sessions_ended_total",
                    {"agent": agent, "outcome": r["status"]},
                    float(r["ended"]),
                )
            )
        for direction, key in (("input", "input_tokens"), ("output", "output_tokens")):
            tokens[(agent, direction)] = tokens.get((agent, direction), 0.0) + float(r[key])
        cost[agent] = cost.get(agent, 0.0) + float(r["cost_usd"])
    for agent, n in sorted(started.items()):
        out.append(("examlops_agent_sessions_started_total", {"agent": agent}, n))
    for agent, n in sorted(steps.items()):
        out.append(("examlops_agent_steps_total", {"agent": agent}, n))
    for (agent, direction), n in sorted(tokens.items()):
        out.append(("examlops_agent_tokens_total", {"agent": agent, "direction": direction}, n))
    for agent, usd in sorted(cost.items()):
        out.append(("examlops_agent_cost_usd_total", {"agent": agent}, usd))
    for r in roll["tools"]:
        calls, ok = float(r["calls"]), float(r["ok"])
        out.append(("examlops_agent_tool_calls_total", {"tool": r["tool"], "outcome": "ok"}, ok))
        out.append(
            (
                "examlops_agent_tool_calls_total",
                {"tool": r["tool"], "outcome": "error"},
                calls - ok,
            )
        )
    for r in roll["anomalies"]:
        out.append(
            (
                "examlops_agent_anomalies_total",
                {"agent": r["agent"], "code": r["code"]},
                float(r["n"]),
            )
        )
    for r in roll["breaker"]:
        event = "tripped" if r["action"].endswith("tripped") else "warning"
        out.append(
            (
                "examlops_agent_breaker_events_total",
                {"event": event, "code": r["target"] or "unknown"},
                float(r["n"]),
            )
        )
    fam = "examlops_agent_session_duration_seconds"
    for agent, h in sorted(roll["durations"].items()):
        for bound, n in zip(DURATION_BUCKETS_S, h["buckets"], strict=True):
            out.append((f"{fam}_bucket", {"agent": agent, "le": f"{bound:g}"}, float(n)))
        out.append((f"{fam}_bucket", {"agent": agent, "le": "+Inf"}, float(h["n"])))
        out.append((f"{fam}_sum", {"agent": agent}, float(h["sum"])))
        out.append((f"{fam}_count", {"agent": agent}, float(h["n"])))
    return out


def export(model: str | None = None, tenant: str = "default") -> str:
    """Everything the platform can publish, as one Prometheus text-format document.

    Each source is collected independently and a failing one is skipped rather than emptying the
    file: a textfile collector that vanishes takes every series with it, and losing the SLO
    gauges because the vector table is unreadable would be a worse outage than the one it reports.
    """
    samples: list[tuple] = []
    for collect in (
        lambda: slo_samples(model, tenant),
        lambda: vector_samples(tenant),
        agent_samples,
    ):
        try:
            samples.extend(collect())
        except Exception:  # noqa: BLE001 - one unreadable source must not blank the export
            continue
    return render(samples)
