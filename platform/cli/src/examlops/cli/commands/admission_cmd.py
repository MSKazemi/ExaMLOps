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
    """Enqueue a work item (durable). A worker claims it under the global + per-tenant caps.

    **This enqueues; it does not dispatch.** `examlops.admission` is a facade whose `dispatch` is
    injected by whatever embeds it, and the control plane runs its own admission accounting on this
    table rather than through the facade — so an item submitted here waits until something claims
    it. `exa admission stats` reports how long the oldest queued item has been waiting, which is
    what tells a busy queue from a stranded one.
    """
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
    _output.ok(
        f"Queued admission #{item_id} ({kind}, tenant={tenant}) — it waits for a worker to claim "
        f"it. Check it is moving with: exa admission stats"
    )


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
