"""``exa workbench`` — Project Workbenches (P5, ADR 0090).

On-demand, project-bound dev environments. Records intent + wiring in ``platform.db`` and injects the
project's Named Connections as env vars; the runtime (JupyterHub/Docker) performs the actual spawn.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.data.audit import write_audit_event
from examlops.workbenches import (
    WorkbenchError,
    create_workbench,
    delete_workbench,
    get_workbench,
    list_workbenches,
    start_workbench,
    stop_workbench,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Project Workbenches — on-demand, project-bound dev environments (P5).",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command()
def create(
    name: str = typer.Argument(..., help="Workbench name"),
    project: str = typer.Option(..., "--project", "-p", help="Owning project (required)"),
    image: str | None = typer.Option(None, "--image", help="Container image"),
    cpu: float | None = typer.Option(None, "--cpu", help="CPU cores"),
    memory_gb: float | None = typer.Option(None, "--memory-gb", help="RAM in GB"),
) -> None:
    """Define a workbench in a project (status STOPPED until started)."""
    if get_workbench(name, project):
        _output.error(f"Workbench '{name}' already exists in project '{project}'")
        raise typer.Exit(1)
    try:
        create_workbench(
            name, project, image=image, cpu=cpu, memory_gb=memory_gb, created_by=_actor()
        )
    except WorkbenchError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    write_audit_event("cli", _actor(), "workbench_created", name, {"project": project})
    _output.ok(f"Workbench '{name}' created in project '{project}'")


@app.command("list")
def workbench_list(
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project"),
) -> None:
    """List workbenches."""
    rows = list_workbenches(project=project)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.warning("No workbenches. Create one: exa workbench create <name> --project <p>")
        return
    _output.print_table(
        "Workbenches",
        ["Name", "Project", "Image", "Status", "Volume"],
        [[r["name"], r["project"], r["image"], r["status"], r["storage_volume"]] for r in rows],
    )


@app.command()
def start(
    name: str = typer.Argument(..., help="Workbench name"),
    project: str = typer.Option(..., "--project", "-p", help="Owning project"),
) -> None:
    """Start a workbench — marks it RUNNING and prints its launch spec (image, volume, injected env)."""
    try:
        spec = start_workbench(name, project, actor=_actor())
    except WorkbenchError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    write_audit_event("cli", _actor(), "workbench_started", name, {"project": project})
    if _output.json_mode:
        _output.print_json(spec)
        return
    _output.ok(f"Workbench '{name}' RUNNING — image {spec['image']} · volume {spec['volume']}")
    _output.info(f"Injected {len(spec['env'])} connection env var(s) from project '{project}'.")


@app.command()
def stop(
    name: str = typer.Argument(..., help="Workbench name"),
    project: str = typer.Option(..., "--project", "-p", help="Owning project"),
) -> None:
    """Stop a workbench (marks STOPPED)."""
    if not stop_workbench(name, project):
        _output.error(f"Workbench '{name}' not found")
        raise typer.Exit(1)
    write_audit_event("cli", _actor(), "workbench_stopped", name, {"project": project})
    _output.ok(f"Workbench '{name}' stopped")


@app.command()
def delete(
    name: str = typer.Argument(..., help="Workbench name"),
    project: str = typer.Option(..., "--project", "-p", help="Owning project"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a workbench definition."""
    if not _output.confirm(f"Delete workbench '{name}'?", auto_yes=yes):
        _output.info("Cancelled.")
        return
    if not delete_workbench(name, project):
        _output.error(f"Workbench '{name}' not found")
        raise typer.Exit(1)
    write_audit_event("cli", _actor(), "workbench_deleted", name, {"project": project})
    _output.ok(f"Workbench '{name}' deleted")
