"""C4 — `exa agentops`: agent trace & tool-call analytics (ADR 0021).

Session-level view of the Skipper agent: tool success rates, recent sessions,
per-session replay, and anomaly detection (loops / step blowups / cost overruns).
Tenant-scoped (D6); tool args are stored already-redacted (D8).
"""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    help="AgentOps — agent trace & tool-call analytics (success, loops, cost, replay)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa agentops tools\n\n"
    "  exa agentops sessions --tenant acme --limit 20\n\n"
    "  exa agentops replay sess-42\n\n"
    "  exa agentops anomalies sess-42"
)


@app.command("tools", epilog=_EXAMPLES)
def tools(
    tenant: str | None = typer.Option(None, "--tenant", help="Filter to one tenant (D6)"),
) -> None:
    """Per-tool success rate, call count, and average latency (R2)."""
    from examlops import platform_db

    rows = platform_db.tool_success_rate(None, tenant=tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No tool-call analytics recorded yet.")
        return
    table = [
        [
            r["tool"],
            str(r["calls"]),
            f"{r['success_rate'] * 100:.0f}%",
            str(r["errors"]),
            f"{r['avg_latency_ms']:.0f}ms" if r["avg_latency_ms"] is not None else "-",
        ]
        for r in rows
    ]
    _output.print_table(
        "Agent Tool Analytics" + (f" — {tenant}" if tenant else ""),
        ["Tool", "Calls", "Success", "Errors", "Avg Latency"],
        table,
    )


@app.command("sessions")
def sessions(
    tenant: str | None = typer.Option(None, "--tenant", help="Filter to one tenant"),
    status: str | None = typer.Option(None, "--status", help="ok | anomaly | error"),
    limit: int = typer.Option(50, "--limit", help="Max sessions (newest first)"),
) -> None:
    """List recent agent sessions with steps, cost, and status (R6 index)."""
    from examlops import platform_db

    rows = platform_db.list_agent_sessions(tenant=tenant, status=status, limit=limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No agent sessions recorded yet.")
        return
    table = [
        [
            r["session_id"],
            r["tenant"],
            r.get("agent") or "-",
            str(r["steps"]),
            str(r["errors"]),
            f"${r['cost_usd']:.4f}",
            r["status"],
        ]
        for r in rows
    ]
    _output.print_table(
        "Agent Sessions",
        ["Session", "Tenant", "Agent", "Steps", "Errors", "Cost", "Status"],
        table,
    )


@app.command("replay")
def replay(
    session_id: str = typer.Argument(..., help="Session id to reconstruct"),
) -> None:
    """Reconstruct a session's tool-call timeline (R6, GWT-5)."""
    from examlops import platform_db

    trace = platform_db.get_agent_session_trace(session_id)
    if trace["session"] is None:
        _output.error(f"Session '{session_id}' not found.")
        return
    if _output.json_mode:
        _output.print_json(trace)
        return
    s = trace["session"]
    _output.info(
        f"Session [bold]{session_id}[/bold] — {s['status']} · {s['steps']} steps · "
        f"${s['cost_usd']:.4f} · tenant {s['tenant']}"
    )
    table = [
        [
            str(st["step"]),
            st["tool"],
            "ok" if st["ok"] else "FAIL",
            (st.get("error") or "")[:40],
            f"{st['latency_ms']:.0f}ms" if st.get("latency_ms") is not None else "-",
        ]
        for st in trace["steps"]
    ]
    _output.print_table(
        f"Timeline — {session_id}",
        ["Step", "Tool", "Result", "Error", "Latency"],
        table,
    )


@app.command("anomalies")
def anomalies(
    session_id: str = typer.Argument(..., help="Session id to analyze"),
    cost_budget: float = typer.Option(1.0, "--cost-budget", help="USD budget for overrun check"),
) -> None:
    """Detect reasoning loops, step blowups, and cost overruns (R4, GWT-3/4)."""
    from examlops.agentops import detect_anomalies

    found = detect_anomalies(session_id, cost_budget=cost_budget)
    if _output.json_mode:
        _output.print_json(
            [
                {"code": a.code, "severity": a.severity, "detail": a.detail, "tool": a.tool}
                for a in found
            ]
        )
        return
    if not found:
        _output.ok(f"No anomalies detected in '{session_id}'.")
        return
    table = [[a.severity.upper(), a.code, a.tool or "-", a.detail] for a in found]
    _output.print_table(
        f"Anomalies — {session_id}",
        ["Severity", "Code", "Tool", "Detail"],
        table,
    )
