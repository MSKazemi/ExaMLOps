from __future__ import annotations

import os

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.platform_db import init_db, write_audit_event

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich", context_settings={"help_option_names": ["-h", "--help"]})

_EXAMPLES_LIST = (
    "Examples:\n\n"
    "  exa approvals list\n\n"
    "  exa approvals list --all"
)
_EXAMPLES_APPROVE = (
    "Examples:\n\n"
    "  exa approvals approve JPCP\n\n"
    "  exa --json approvals approve JPCP"
)
_EXAMPLES_REJECT = (
    "Examples:\n\n"
    "  exa approvals reject JPCP\n\n"
    '  exa approvals reject JPCP --reason "needs data review"'
)


@app.command("list", epilog=_EXAMPLES_LIST)
def list_approvals(all: bool = typer.Option(False, "--all", help="Show all statuses, not just pending")):
    """List model change approvals."""
    cfg = load_config()
    url = f"{cfg.control_plane_url}/approvals"
    if not all:
        url += "?status=pending"
    try:
        rows_raw = _client.get(url)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    rows = [[r["model_id"], (r["commit_sha"] or "")[:8], r["commit_msg"] or "",
             r["status"], r["requested_at"]] for r in rows_raw]
    _output.print_table(
        "Pending Approvals" if not all else "All Approvals",
        ["Model", "Commit", "Message", "Status", "Requested"],
        rows,
    )


@app.command(epilog=_EXAMPLES_APPROVE)
def approve(model: str = typer.Argument(..., help="Model ID to approve (e.g. JPCP)")):
    """Approve a pending model change — fires Prefect training immediately."""
    cfg = load_config()
    try:
        result = _client.post(f"{cfg.control_plane_url}/approve/{model}", {}, token=cfg.control_plane_token)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    try:
        init_db()
        write_audit_event("cli", actor, "model_approved", model,
                          {"flow_run_id": result.get("flow_run_id")})
    except Exception:
        pass
    _output.ok(f"Approved {model} — flow_run_id: {result.get('flow_run_id')}")
    if _output.json_mode:
        _output.print_json(result)


@app.command(epilog=_EXAMPLES_REJECT)
def reject(
    model: str = typer.Argument(..., help="Model ID to reject"),
    reason: str | None = typer.Option(None, "--reason", "-r", help="Rejection reason"),
):
    """Reject a pending model change — no training will run."""
    cfg = load_config()
    try:
        result = _client.post(
            f"{cfg.control_plane_url}/reject/{model}",
            {"reason": reason},
            token=cfg.control_plane_token,
        )
    except _client.ClientError as e:
        _output.error(str(e))
        return
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    try:
        init_db()
        write_audit_event("cli", actor, "model_rejected", model, {"reason": reason or ""})
    except Exception:
        pass
    _output.ok(f"Rejected {model}" + (f" — reason: {reason}" if reason else ""))
    if _output.json_mode:
        _output.print_json(result)


_EXAMPLES_DELETE = (
    "Examples:\n\n"
    "  exa approvals delete <uuid>"
)

@app.command("delete", epilog=_EXAMPLES_DELETE)
def delete(
    approval_id: str = typer.Argument(..., help="Approval UUID to delete"),
):
    """Delete a pending approval by its UUID."""
    cfg = load_config()
    try:
        result = _client.delete(
            f"{cfg.control_plane_url}/approvals/{approval_id}",
            token=cfg.control_plane_token,
        )
    except _client.ClientError as e:
        _output.error(str(e))
        return
    _output.ok(f"Deleted approval {approval_id}")
    if _output.json_mode:
        _output.print_json(result)
