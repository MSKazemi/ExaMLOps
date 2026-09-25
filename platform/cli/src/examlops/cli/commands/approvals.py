from __future__ import annotations

import os

import typer

from examlops import control_plane_api
from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._provenance import reason_option
from examlops.data import init_db
from examlops.data.audit import audit_best_effort

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
    try:
        rows_raw = control_plane_api.list_approvals(
            status=None if all else "pending",
            base=cfg.control_plane_url,
            token=cfg.control_plane_token,
        )
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
    reason: str | None = reason_option(),
) -> None:
    """Approve a pending model change — fires Prefect training immediately."""
    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "would_approve": model})
        else:
            _output.info(f"Dry run — would approve {model} and schedule training (nothing fired).")
        return
    from examlops import policy
    from examlops.sdk import models as sdk_models
    from examlops.sdk.errors import PolicyDeniedError, SDKError

    # Peek at the `model_approve` decision (unaudited — the SDK call below records it once) so
    # the prompt can say a rule requires approval and default to NO, as `exa retrain` does.
    # Without this a require_approval rule was satisfied by a default-yes Enter on a prompt that
    # never mentioned it. The context is the SDK's own, so a `when:` condition decides alike.
    peek = policy.decide_safe(
        "model_approve",
        {"model": model, "actor": sdk_models._actor()},
        default_effect=policy.DENY,
        audit=False,
    )
    approval_note = " [policy requires approval]" if peek.requires_approval else ""
    if not _output.confirm(
        f"Approve [bold]{model}[/bold] and schedule training now?{approval_note}",
        default=not peek.requires_approval,
    ):
        _output.warning("Aborted — nothing approved.")
        raise typer.Exit(0)
    # One path with the SDK (ADR 0078 clause 2): `examlops.models.approve()` consults the
    # `model_approve` policy rule (no rule ⇒ allow, unaudited), calls the control plane and writes
    # the `model_approved` audit event. The human answered a prompt that named a
    # require_approval rule, so the approval is given here, as it is for `exa retrain`.
    with _output.spinner(f"Approving {model} and scheduling training…"):
        try:
            outcome = sdk_models.approve(
                model, confirm=True, approved=True, reason=reason, source="cli"
            )
        except PolicyDeniedError as e:
            _output.error(str(e), hint="See your policy.yaml or run: exa policy list")
        except SDKError as e:
            _output.error(f"Failed to approve {model}: {e}")
    result = outcome.raw
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
        result = control_plane_api.reject(
            model,
            body={"reason": reason or ""},
            base=cfg.control_plane_url,
            token=cfg.control_plane_token,
        )
    except _client.ClientError as e:
        _output.error(f"Failed to reject {model}: {e}")
        return
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    try:
        init_db()
    except Exception:  # noqa: BLE001 - `audit_best_effort` reports the write it then cannot make
        pass
    audit_best_effort("cli", actor, "model_rejected", model, {"reason": reason or ""})
    _output.ok(f"Rejected {model}" + (f" — reason: {reason}" if reason else ""))
    if _output.json_mode:
        _output.print_json(result)


@app.command("delete", epilog=_EXAMPLES_DELETE)
def delete(
    approval_id: str = typer.Argument(..., help="Approval UUID to retract"),
) -> None:
    """Retract a pending approval by its UUID (a stale or duplicate entry).

    The approval is kept in the history, marked `retracted` with who and when; nothing is erased.
    """
    if not _output.confirm(f"Retract approval [bold]{approval_id[:8]}…[/bold]?"):
        _output.info("Cancelled.")
        return
    cfg = load_config()
    try:
        result = control_plane_api.retract_approval(
            approval_id, base=cfg.control_plane_url, token=cfg.control_plane_token
        )
    except _client.ClientError as e:
        _output.error(f"Failed to retract approval: {e}")
        return
    _output.ok(f"Retracted approval {approval_id[:8]}… (kept in the history as `retracted`)")
    if _output.json_mode:
        _output.print_json(result)
