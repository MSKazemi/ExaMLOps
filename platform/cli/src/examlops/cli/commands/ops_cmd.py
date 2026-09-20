"""`exa ops` — operation handles for long-running work (ADR 0147 decision 5).

Anything that outlives the call that started it (a retrain, a queued pipeline dispatch) returns an
operation id — the control plane's command id. These commands read it, wait for it (bounded) and
cancel it while it is still queued. There is no second store: an operation is a
``control_plane_commands`` record, reached through ``/v1/commands``. The logic is in
:mod:`examlops.operations`, shared with the ``operation_status`` / ``operation_cancel`` MCP tools.

Exit codes of ``exa ops wait``: 0 completed, 1 failed or cancelled, 124 timed out (the operation
keeps running), other non-zero = could not be read.
"""

from __future__ import annotations

import os
from typing import Any

import typer

from examlops import operations
from examlops.cli import _output

app = typer.Typer(
    help="Operation handles — status, wait and cancel for long-running work",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

TIMEOUT_EXIT = 124

_STATES = "working | input_required | completed | failed | cancelled"
_EX_LIST = (
    "Examples:\n\n  exa ops list\n\n  exa ops list --state working\n\n"
    "  exa -o json ops list --limit 10"
)
_EX_STATUS = (
    "Examples:\n\n  exa ops status v1:retrain:4f0c1e2a…\n\n  exa -o json ops status <op-id>"
)
_EX_WAIT = (
    "Examples:\n\n  exa ops wait v1:retrain:4f0c1e2a… --timeout 600\n\n"
    "  exa -o json ops wait <op-id> --timeout 0   # look once, never block\n\n"
    "Exit codes: 0 completed, 1 failed/cancelled, 124 timed out."
)
_EX_CANCEL = "Examples:\n\n  exa ops cancel v1:retrain:4f0c1e2a…\n\n  exa --yes ops cancel <op-id>"

_COLUMNS = ["operation_id", "kind", "state", "raw_state", "run_state", "updated_at"]


def _fail(out: dict[str, Any]) -> None:
    """Leave the command on a structured library error (one JSON document under -o json)."""
    _output.error(str(out.get("error") or out.get("code")), hint=str(out.get("code") or ""))


def _record(op: dict[str, Any]) -> dict[str, Any]:
    rec = {
        "operation": op["operation_id"],
        "kind": op["kind"],
        "state": op["state"],
        "cancellable": op["cancellable"],
        "control-plane state": op["raw_state"],
        "flow run": op["flow_run_id"] or "—",
        "flow run state": op["run_state"] or "—",
        "attempts": op["attempts"],
        "last error": op["last_error"] or "—",
        "updated": op["updated_at"],
    }
    if op.get("detail"):
        rec["detail"] = op["detail"]
    return rec


@app.command("list", epilog=_EX_LIST)
def list_cmd(
    state: str | None = typer.Option(None, "--state", help=f"Filter: {_STATES}"),
    kind: str | None = typer.Option(None, "--kind", help="Filter by kind, e.g. retrain"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Max operations"),
) -> None:
    """List operations of your tenant, newest first."""
    out = operations.list_operations(state=state, kind=kind, limit=limit)
    if not out["ok"]:
        _fail(out)
    rows = out["operations"]
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No operations found")
        return
    _output.print_table("Operations", _COLUMNS, [[o.get(k) or "" for k in _COLUMNS] for o in rows])
    if out["truncated"]:
        _output.info("More exist than were scanned; narrow with --state or --kind")


@app.command("status", epilog=_EX_STATUS)
def status_cmd(
    operation_id: str = typer.Argument(..., help="Operation id (the command id a call returned)"),
) -> None:
    """Show one operation's state, flow run and last error."""
    out = operations.status(operation_id)
    if not out["ok"]:
        _fail(out)
    if _output.json_mode:
        _output.print_json(out["operation"])
        return
    _output.print_record(_record(out["operation"]))


@app.command("wait", epilog=_EX_WAIT)
def wait_cmd(
    operation_id: str = typer.Argument(..., help="Operation id to wait for"),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        min=0,
        help="Seconds to wait (default EXAMLOPS_OPS_WAIT_TIMEOUT, 300; 0 = look once)",
    ),
    interval: float | None = typer.Option(
        None, "--interval", min=0.05, help="Seconds between polls (default 2)"
    ),
) -> None:
    """Wait for an operation to finish, at most --timeout seconds; never blocks forever."""
    out = operations.wait(operation_id, timeout=timeout, interval=interval)
    if not out["ok"]:
        _fail(out)
    op = out["operation"]
    if _output.json_mode:
        _output.print_json({**op, "timed_out": out["timed_out"]})
    elif out["timed_out"]:
        _output.warning(
            f"Timed out; operation {op['operation_id']} is still {op['state']} — it keeps running"
        )
        _output.print_record(_record(op))
    else:
        _output.print_record(_record(op))
    if out["timed_out"]:
        raise typer.Exit(TIMEOUT_EXIT)
    if op["state"] != "completed":
        raise typer.Exit(1)


@app.command("cancel", epilog=_EX_CANCEL)
def cancel_cmd(
    operation_id: str = typer.Argument(..., help="Operation to cancel (must still be queued)"),
) -> None:
    """Cancel an operation that has not been dispatched yet (queued or awaiting retry)."""
    if not _output.confirm(f"Cancel operation [bold]{operation_id}[/bold]?"):
        _output.info("Cancelled nothing.")
        return
    out = operations.cancel(operation_id)
    _audit(operation_id, out)
    if not out["ok"]:
        if _output.json_mode:
            _output.print_json(out)
            raise typer.Exit(1)
        _fail(out)
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"Operation {operation_id} cancelled")
    _output.print_record(_record(out["operation"]))


def _audit(operation_id: str, out: dict[str, Any]) -> None:
    """Record the request — including a refused one — best-effort, never failing the command."""
    from examlops.data.audit import audit_best_effort

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    audit_best_effort(
        "exa-ops",
        actor,
        "operation_cancel_requested",
        operation_id,
        {"cancelled": bool(out.get("cancelled")), "code": out.get("code", "ok")},
    )
