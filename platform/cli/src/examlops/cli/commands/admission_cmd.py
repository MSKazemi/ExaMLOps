"""``exa admission`` — durable fair-share admission-control queue (Phase 1 item 1.5)."""

from __future__ import annotations

import json

import typer

from examlops.cli import _output

app = typer.Typer(no_args_is_help=True, help="Admission-control queue (per-tenant fair-share)")


@app.command("submit")
def submit(
    kind: str = typer.Argument(..., help="Work kind, e.g. retrain | pipeline"),
    payload: str = typer.Option("{}", "--payload", "-p", help="JSON payload"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant for fair-share accounting"),
    project: str | None = typer.Option(None, "--project", help="Project attribution"),
    priority: int = typer.Option(0, "--priority", help="Higher runs first within a tenant"),
) -> None:
    """Enqueue a work item (durable; drained under the global + per-tenant caps)."""
    from examlops import admission

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        _output.error(f"--payload must be valid JSON: {exc}")
        raise typer.Exit(1) from exc
    item_id = admission.submit(kind, data, tenant=tenant, project=project, priority=priority)
    if _output.json_mode:
        _output.print_json({"id": item_id, "kind": kind, "tenant": tenant})
        return
    _output.ok(f"Queued admission #{item_id} ({kind}, tenant={tenant}).")


@app.command("stats")
def stats() -> None:
    """Show queue depth by state (queued/running/done/rejected/failed)."""
    from examlops import admission

    s = admission.stats()
    if _output.json_mode:
        _output.print_json(s)
        return
    _output.print_table(
        "Admission queue",
        ["State", "Count"],
        [[k, str(v)] for k, v in s.items()],
    )
