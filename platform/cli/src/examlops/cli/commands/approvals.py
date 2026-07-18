from __future__ import annotations

import os

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.data import init_db
from examlops.data.audit import write_audit_event

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_LIST = "Examples:\n\n  exa approvals list\n\n  exa approvals list --all"
_EXAMPLES_APPROVE = (
    "Examples:\n\n  exa approvals approve JPCP\n\n  exa --json approvals approve JPCP"
)
_EXAMPLES_REJECT = (
    "Examples:\n\n"
    "  exa approvals reject JPCP\n\n"
    '  exa approvals reject JPCP --reason "needs data review"\n\n'
    "  exa --yes approvals reject JPCP --reason automated"
)
_EXAMPLES_DELETE = (
    "Examples:\n\n  exa approvals delete <uuid>\n\n  exa --yes approvals delete <uuid>"
)


@app.command("list", epilog=_EXAMPLES_LIST)
def list_approvals(
    all: bool = typer.Option(False, "--all", help="Show all statuses, not just pending"),
) -> None:
    """List model change approvals."""
    cfg = load_config()
    url = f"{cfg.control_plane_url}/approvals"
    if not all:
        url += "?status=pending"
    try:
        rows_raw = _client.get(url, token=cfg.control_plane_token)
    except _client.ClientError as e:
        _output.error(
            f"Failed to list approvals: {e}", hint="Is the control plane running? exa status"
        )
        return
    if not rows_raw:
        _output.ok("No approvals found")
        return
    rows = [
        [
            r["model_id"],
            (r.get("commit_sha") or "")[:8],
            (r.get("commit_msg") or "")[:50],
            r["status"],
            (r.get("requested_at") or "")[:16],
        ]
        for r in rows_raw
    ]
    _output.print_table(
        "Pending Approvals" if not all else "All Approvals",
        ["Model", "Commit", "Message", "Status", "Requested"],
        rows,
    )


@app.command(epilog=_EXAMPLES_APPROVE)
def approve(
    model: str = typer.Argument(..., help="Model ID to approve (e.g. JPCP)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be approved without firing training"
    ),
) -> None:
    """Approve a pending model change — fires Prefect training immediately."""
    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "would_approve": model})
        else:
            _output.info(f"Dry run — would approve {model} and schedule training (nothing fired).")
        return
    if not _output.confirm(
        f"Approve [bold]{model}[/bold] and schedule training now?", default=True
    ):
        _output.warning("Aborted — nothing approved.")
        raise typer.Exit(0)
    cfg = load_config()
    with _output.spinner(f"Approving {model} and scheduling training…"):
        try:
            result = _client.post(
                f"{cfg.control_plane_url}/approve/{model}",
                {},
                token=cfg.control_plane_token,
            )
        except _client.ClientError as e:
            _output.error(f"Failed to approve {model}: {e}")
            return
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    try:
        init_db()
        write_audit_event(
            "cli", actor, "model_approved", model, {"flow_run_id": result.get("flow_run_id")}
        )
    except Exception:
        pass
    _output.ok(f"Approved {model}")
    _output.print_record(
        {
            "flow_run_id": result.get("flow_run_id", "—"),
            "status": result.get("status", "scheduled"),
        }
    )
    _output.hint("Monitor progress: exa status")


@app.command(epilog=_EXAMPLES_REJECT)
def reject(
    model: str = typer.Argument(..., help="Model ID to reject"),
    reason: str | None = typer.Option(None, "--reason", "-r", help="Rejection reason"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be rejected without changing anything"
    ),
) -> None:
    """Reject a pending model change — no training will run."""
    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "would_reject": model, "reason": reason or ""})
        else:
            _output.info(
                f"Dry run — would reject {model}" + (f" (reason: {reason})" if reason else "")
            )
        return
    if not _output.confirm(
        f"Reject pending approval for [bold]{model}[/bold]? This cannot be undone."
    ):
        _output.info("Cancelled.")
        return
    cfg = load_config()
    try:
        result = _client.post(
            f"{cfg.control_plane_url}/reject/{model}",
            {"reason": reason or ""},
            token=cfg.control_plane_token,
        )
    except _client.ClientError as e:
        _output.error(f"Failed to reject {model}: {e}")
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


@app.command("delete", epilog=_EXAMPLES_DELETE)
def delete(
    approval_id: str = typer.Argument(..., help="Approval UUID to delete"),
) -> None:
    """Delete a pending approval by its UUID (retract a stale or duplicate entry)."""
    if not _output.confirm(f"Delete approval [bold]{approval_id[:8]}…[/bold]?"):
        _output.info("Cancelled.")
        return
    cfg = load_config()
    try:
        result = _client.delete(
            f"{cfg.control_plane_url}/approvals/{approval_id}",
            token=cfg.control_plane_token,
        )
    except _client.ClientError as e:
        _output.error(f"Failed to delete approval: {e}")
        return
    _output.ok(f"Deleted approval {approval_id[:8]}…")
    if _output.json_mode:
        _output.print_json(result)
