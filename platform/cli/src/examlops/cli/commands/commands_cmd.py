"""`exa commands` — follow asynchronous control-plane commands (`/v1/commands`, plan P1.2).

`exa retrain --async` submits a retrain as a durable command and returns at once; the control
plane's worker pool dispatches it, retrying with backoff and burying it (`dead`) after a bounded
number of attempts. These commands read, list and cancel them.
"""

from __future__ import annotations

import urllib.parse

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_LIST = (
    "Examples:\n\n"
    "  exa commands list\n\n"
    "  exa commands list --state failed\n\n"
    "  exa --json commands list --limit 100"
)
_EXAMPLES_SHOW = "Examples:\n\n  exa commands show v1:retrain:4f0c1e2a…\n\n  exa --json commands show v1:retrain:4f0c1e2a…"
_EXAMPLES_CANCEL = "Examples:\n\n  exa commands cancel v1:retrain:4f0c1e2a…\n\n  exa --yes commands cancel v1:retrain:4f0c1e2a…"

_COLUMNS = ["command_id", "kind", "state", "run_state", "attempts", "updated_at"]


def _quoted(command_id: str) -> str:
    return urllib.parse.quote(command_id, safe="")


@app.command("list", epilog=_EXAMPLES_LIST)
def list_commands(
    state: str | None = typer.Option(
        None, "--state", help="pending | dispatching | failed | succeeded | dead | cancelled"
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="Page size"),
    cursor: str | None = typer.Option(None, "--cursor", help="Continue from a previous page"),
) -> None:
    """List asynchronous commands in your tenant, newest first."""
    cfg = load_config()
    params = {"limit": str(limit)}
    if state:
        params["state"] = state
    if cursor:
        params["cursor"] = cursor
    try:
        page = _client.get(
            f"{cfg.control_plane_url}/v1/commands?{urllib.parse.urlencode(params)}",
            token=cfg.control_plane_token,
        )
    except _client.ClientError as exc:
        _output.error(f"Could not list commands: {exc}", hint="Is the control plane up? exa status")
        return
    if _output.json_mode:
        _output.print_json(page)
        return
    items = page.get("items", [])
    if not items:
        _output.ok("No commands found")
        return
    _output.print_table("Commands", _COLUMNS, [[c.get(k, "") for k in _COLUMNS] for c in items])
    if page.get("next_cursor"):
        _output.info(f"More: exa commands list --cursor {page['next_cursor']}")


@app.command("show", epilog=_EXAMPLES_SHOW)
def show_command(
    command_id: str = typer.Argument(..., help="Id printed by `exa retrain --async`"),
) -> None:
    """Show one command: its state, attempts, result (the flow run) or last error."""
    cfg = load_config()
    try:
        view = _client.get(
            f"{cfg.control_plane_url}/v1/commands/{_quoted(command_id)}",
            token=cfg.control_plane_token,
        )
    except _client.ClientError as exc:
        _output.error(f"Could not read command {command_id}: {exc}")
        return
    result = view.get("result") or {}
    _output.print_record(
        {
            **{k: view.get(k) for k in ("command_id", "kind", "state", "attempts", "updated_at")},
            "flow_run_id": result.get("flow_run_id"),
            "run_state": view.get("run_state"),
            "last_error": view.get("last_error"),
        }
    )


@app.command("cancel", epilog=_EXAMPLES_CANCEL)
def cancel_command(command_id: str = typer.Argument(..., help="Command to cancel")) -> None:
    """Cancel a command that has not been dispatched yet (pending or awaiting retry)."""
    if not _output.confirm(f"Cancel command [bold]{command_id}[/bold]?"):
        _output.info("Cancelled nothing.")
        return
    cfg = load_config()
    try:
        view = _client.delete(
            f"{cfg.control_plane_url}/v1/commands/{_quoted(command_id)}",
            token=cfg.control_plane_token,
        )
    except _client.ClientError as exc:
        _output.error(f"Could not cancel {command_id}: {exc}")
        return
    _output.ok(f"Command {command_id} cancelled")
    if _output.json_mode:
        _output.print_json(view)
