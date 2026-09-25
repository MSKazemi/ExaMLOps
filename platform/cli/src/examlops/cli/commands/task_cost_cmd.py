"""`exa finops task-cost` — the per-task agent cost ledger (ADR 0148 decision 4)."""

from __future__ import annotations

import os
from typing import Any

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Per-task agent cost ledger: model/tool calls, sandbox, idle state, standby (ADR 0148)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa finops task-cost record T1 --project research --component sandbox_seconds "
    "--quantity 120 --rate 0.0001\n\n"
    "  exa finops task-cost record T1 --project research --component model_call --cost 0.02\n\n"
    "  exa finops task-cost apportion gpu-pool-a --cost 4.8 --project research --task T1 "
    "--task T2 --period 2026-09-25\n\n"
    "  exa finops task-cost show T1"
)


@app.command("record", epilog=_EXAMPLES)
def record(
    task_id: str = typer.Argument(..., help="Agent task id"),
    project: str = typer.Option(..., "--project", help="Owning project (required)"),
    component: str = typer.Option(
        ...,
        "--component",
        help="model_call | tool_call | sandbox_seconds | idle_state_gb_hours",
    ),
    cost: float = typer.Option(None, "--cost", help="Cost in USD (model_call / tool_call)"),
    quantity: float = typer.Option(
        None, "--quantity", help="sandbox seconds, or GB for idle_state_gb_hours"
    ),
    hours: float = typer.Option(None, "--hours", help="idle_state_gb_hours: hours held idle"),
    rate: float = typer.Option(
        None, "--rate", help="USD per second / per GB-hour (else the EXAMLOPS_* rate env var)"
    ),
    entry_id: str = typer.Option(None, "--entry-id", help="Idempotency key for this entry"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Record one metered cost entry for an agent task."""
    from examlops.finops import task_ledger as tl

    res: dict[str, Any]
    try:
        if component == "sandbox_seconds":
            if quantity is None:
                _output.error("--quantity (seconds) is required for sandbox_seconds")
            res = tl.record_sandbox(
                task_id,
                quantity,
                project=project,
                usd_per_second=rate,
                tenant=tenant,
                entry_id=entry_id,
            )
        elif component == "idle_state_gb_hours":
            if quantity is None or hours is None:
                _output.error("--quantity (GB) and --hours are required for idle_state_gb_hours")
            res = tl.record_idle_state(
                task_id,
                quantity,
                hours,
                project=project,
                usd_per_gb_hour=rate,
                tenant=tenant,
                entry_id=entry_id,
            )
        elif component in ("model_call", "tool_call"):
            if cost is None:
                _output.error(f"--cost is required for {component}")
            res = tl.record_entry(
                task_id,
                component,
                cost,
                project=project,
                quantity=1.0 if quantity is None else quantity,
                tenant=tenant,
                entry_id=entry_id,
            )
        else:
            _output.error(
                f"component {component!r} cannot be recorded here "
                "(hot_pool_standby comes from `exa finops task-cost apportion`)"
            )
    except tl.TaskLedgerError as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(res)
        return
    state = "recorded" if res["created"] else "already recorded"
    _output.ok(f"{component} for {task_id}: {res['cost_usd']:.6f} USD {state}")


@app.command("apportion", epilog=_EXAMPLES)
def apportion(
    pool: str = typer.Argument(..., help="Hot pool / warm sandbox pool name"),
    cost: float = typer.Option(..., "--cost", help="The pool's standby cost in USD"),
    project: str = typer.Option(..., "--project", help="Owning project (required)"),
    task: list[str] = typer.Option(..., "--task", help="Task served (repeat); TASK=WEIGHT ok"),
    rule: str = typer.Option("equal", "--rule", help="equal | weighted"),
    period: str = typer.Option("", "--period", help="Period label; one apportioning per period"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Split a pool's standby cost across its tasks by a declared rule (audited)."""
    from examlops.finops import task_ledger as tl

    tasks: dict[str, float] = {}
    for t in task:
        name, _, w = t.partition("=")
        try:
            tasks[name] = float(w) if w else 1.0
        except ValueError:
            _output.error(f"bad task weight in {t!r}")
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    try:
        res = tl.apportion_standby(
            pool,
            cost,
            tasks,
            project=project,
            rule=rule,
            tenant=tenant,
            period=period,
            actor=actor,
        )
    except tl.TaskLedgerError as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(res)
        return
    _output.print_table(
        f"Standby of {pool} ({rule}, {res['pool_cost_usd']:g} USD)",
        ["Task", "Share USD"],
        [[k, f"{v:.6f}"] for k, v in res["shares"].items()],
    )


@app.command("show", epilog=_EXAMPLES)
def show(
    task_id: str = typer.Argument(..., help="Agent task id"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Show one task's ledger: per-component totals, total, and what is unmetered."""
    from examlops.finops.task_ledger import task_cost

    res = task_cost(task_id, tenant=tenant)
    if _output.json_mode:
        _output.print_json(res)
        return
    if not res["entries"]:
        _output.info(f"No cost entries for task {task_id}")
        return
    _output.print_table(
        f"Task {task_id} — {res['total_usd']:.6f} USD",
        ["Component", "USD"],
        [[c, f"{v:.6f}"] for c, v in res["components"].items()],
    )
    if res["unmetered"]:
        _output.warning("unmetered (total is a lower bound): " + ", ".join(res["unmetered"]))
