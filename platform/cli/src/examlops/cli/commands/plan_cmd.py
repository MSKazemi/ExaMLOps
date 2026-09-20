"""`exa plan` — read stored agent plans (ADR 0147 decision 2).

Plans are created and applied through the MCP tools ``plan_change`` / ``apply_plan``; this
command lets an operator see what an agent proposed and what became of it. It never applies.
"""

from __future__ import annotations

from datetime import datetime

import typer

from examlops import plans
from examlops.cli import _output

app = typer.Typer(
    help="Agent plans — what an agent proposed, its blast radius, and what was applied",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@app.command(
    "list",
    epilog="Examples:\n\n  exa plan list\n\n  exa plan list --state planned\n\n"
    "  exa -o json plan list --limit 10",
)
def list_cmd(
    state: str | None = typer.Option(
        None,
        "--state",
        help="Filter: planned | applying | applied | failed | expired | rejected",
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Max plans to show"),
) -> None:
    """List stored agent plans, newest first."""
    out = plans.list_plans(state, limit)
    if not out["ok"]:
        _output.error(out["error"])
    if _output.json_mode:
        _output.print_json(out["plans"])
        return
    _output.print_table(
        "Agent plans",
        ["Plan", "Tool", "State", "Change", "Expires"],
        [
            [
                p["plan_hash"][:12],
                p["tool"],
                p["state"],
                p["intended_change"],
                _when(p["expires_at"]),
            ]
            for p in out["plans"]
        ],
    )


@app.command(
    "show",
    epilog="Examples:\n\n  exa plan show 3f9c2a1b7d10\n\n  exa -o json plan show <plan_hash>",
)
def show_cmd(
    plan_hash: str = typer.Argument(..., help="Full plan_hash, or a unique prefix"),
) -> None:
    """Show one plan: intended change, blast radius, approvals, preconditions and outcome."""
    from examlops.data import plans as store

    store.init_db()
    full = plan_hash
    if len(plan_hash) < 64:
        matches = [p for p in store.list_plans(None, 1000) if p["plan_hash"].startswith(plan_hash)]
        if len(matches) != 1:
            _output.error(
                f"{len(matches)} plans match {plan_hash!r}", hint="give a longer, unique prefix"
            )
        full = matches[0]["plan_hash"]
    out = plans.get_plan(full)
    if not out["ok"]:
        _output.error(out["error"])
    plan = out["plan"]
    if _output.json_mode:
        _output.print_json(plan)
        return
    _output.print_record(
        {
            "plan": plan["plan_hash"][:12],
            "tool": plan["tool"],
            "state": plan["state"],
            "intended change": plan["intended_change"],
            "args": plan["args"],
            "blast radius": plan["blast_radius"],
            "required approvals": plan["required_approvals"] or "none",
            "approved": plan["approved"],
            "expires": _when(plan["expires_at"]),
            "preconditions": plan["preconditions"],
        },
    )
