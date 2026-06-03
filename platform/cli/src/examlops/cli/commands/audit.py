from __future__ import annotations

import json
from datetime import datetime, timedelta

import typer

from examlops.cli import _output
from examlops.platform_db import get_db, init_db

_EXAMPLES = (
    "Examples:\n\n"
    "  exa audit\n\n"
    "  exa audit --last 7d\n\n"
    "  exa audit --model JPCP\n\n"
    "  exa audit --action model_approved\n\n"
    "  exa --json audit --last 30d"
)


def _parse_days(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    return int(s)


def audit(
    last: str = typer.Option("30d", "--last", help="Time window (e.g. 7d, 30d)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Filter by target model"),
    action: str | None = typer.Option(None, "--action", "-a", help="Filter by action type"),
    source: str | None = typer.Option(None, "--source", "-s", help="Filter by source (cli/agent/bridge)"),
    limit: int = typer.Option(100, "--limit", "-n", help="Max events to show"),
):
    """Show platform audit log — who did what and when."""
    init_db()
    days = _parse_days(last)
    since = datetime.utcnow() - timedelta(days=days)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    query = "SELECT id, ts, source, actor, action, target, details FROM audit_events WHERE ts >= ?"
    params: list = [since_str]
    if model:
        query += " AND target=?"
        params.append(model)
    if action:
        query += " AND action=?"
        params.append(action)
    if source:
        query += " AND source=?"
        params.append(source)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        _output.ok("No audit events found for the given filters")
        return

    if _output.json_mode:
        _output.print_json([
            {
                "id": r["id"],
                "ts": r["ts"],
                "source": r["source"],
                "actor": r["actor"],
                "action": r["action"],
                "target": r["target"],
                "details": json.loads(r["details"]) if r["details"] else None,
            }
            for r in rows
        ])
        return

    cols = ["Time", "Source", "Actor", "Action", "Target", "Details"]
    table_rows = [
        [
            r["ts"],
            r["source"] or "—",
            r["actor"] or "—",
            r["action"],
            r["target"] or "—",
            (r["details"] or "")[:50],
        ]
        for r in rows
    ]
    _output.print_table(f"Audit Log (last {last})", cols, table_rows)
