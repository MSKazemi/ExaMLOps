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
    hardware_profile: str | None = typer.Option(
        None,
        "--hardware-profile",
        help=(
            "Named hardware profile supplying cpu/memory-gb defaults (exa hardware profile list). "
            "Its applicability must include 'workbench' or 'any'. Explicit --cpu/--memory-gb win."
        ),
    ),
) -> None:
    """Define a workbench in a project (status STOPPED until started)."""
    if get_workbench(name, project):
        _output.error(f"Workbench '{name}' already exists in project '{project}'")
        raise typer.Exit(1)
    try:
        row = create_workbench(
            name,
            project,
            image=image,
            cpu=cpu,
            memory_gb=memory_gb,
            hardware_profile=hardware_profile,
            created_by=_actor(),
        )
    except WorkbenchError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    details: dict[str, object] = {"project": project}
    if row.get("hardware_profile"):
        details["hardware_profile"] = row["hardware_profile"]
        details["hardware_profile_version"] = row.get("hardware_profile_version")
    write_audit_event("cli", _actor(), "workbench_created", name, details)
    _output.ok(f"Workbench '{name}' created in project '{project}'")
    if row.get("hardware_profile"):
        _output.info(
            f"Hardware profile '{row['hardware_profile']}' v{row.get('hardware_profile_version')}"
            f" → cpu={row.get('cpu')} memory_gb={row.get('memory_gb')}"
        )
        # A profile is a default, not an override — say which field the flags took back, so a
        # partial application is never silent.
        overridden = [
            flag
            for flag, given in (("--cpu", cpu), ("--memory-gb", memory_gb))
            if given is not None
        ]
        if overridden:
            _output.warning(
                f"{' and '.join(overridden)} given explicitly — those win over the profile; "
                "every other field came from it."
            )


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
        ["Name", "Project", "Image", "Status", "Volume", "Profile"],
        [
            [
                r["name"],
                r["project"],
                r["image"],
                r["status"],
                r["storage_volume"],
                _profile_cell(r),
            ]
            for r in rows
        ],
    )


def _profile_cell(row: dict) -> str:
    """The hardware profile a workbench was created from, for the list table (ADR 0157 Phase 2).

    ``-`` for a workbench created without one — which is every row written before profiles
    existed, and every row still created without the flag.
    """
    name = row.get("hardware_profile")
    if not name:
        return "-"
    version = row.get("hardware_profile_version")
    return f"{name} v{version}" if version is not None else str(name)


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


_EXAMPLES_EXPORT_PIPELINE = (
    "Examples:\n\n"
    "  # Export the tagged cells of a notebook to a pipeline file, then compile it\n"
    "  exa workbench export-pipeline toy_notebook.ipynb\n\n"
    "  # Choose where the generated pipeline lands\n"
    "  exa workbench export-pipeline toy_notebook.ipynb --out flows/toy.py\n\n"
    "  # Also lower the compiled IR to the per-model registry YAML\n"
    "  exa workbench export-pipeline toy_notebook.ipynb --out flows/toy.py --yaml toy.yaml\n\n"
    "  # Machine-readable report (name, hash, steps, dropped cells)\n"
    "  exa workbench export-pipeline toy_notebook.ipynb --json"
)


@app.command("export-pipeline", epilog=_EXAMPLES_EXPORT_PIPELINE)
def export_pipeline(
    notebook: str = typer.Argument(..., help="Tagged .ipynb notebook to read (never executed)"),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Write the generated pipeline here (default: <stem>_pipeline.py)"
    ),
    yaml_path: str | None = typer.Option(
        None, "--yaml", help="Also lower the compiled IR to the per-model registry YAML here"
    ),
    name: str | None = typer.Option(
        None, "--name", help="Pipeline name (default: the notebook's stem)"
    ),
) -> None:
    """Turn tagged notebook cells into a @pipeline file, then compile it (ADR 0160).

    Cells are read as data — the notebook is parsed, never executed. Tag a code cell with one of [bold]param[/bold], [bold]dataset[/bold], [bold]train[/bold], [bold]evaluate[/bold], [bold]promote[/bold] or [bold]skip-export[/bold] (standard Jupyter cell tags); every other code cell is dropped and reported by index, so nothing vanishes unnoticed.

    [bold]One-directional by design:[/bold] editing the generated file does NOT flow back into the notebook, and re-running this command overwrites the file rather than merging. The output is a starting point for the ordinary pipeline-as-code workflow (`exa pipeline compile`), exactly like a hand-written pipeline file.
    """
    from examlops.cli.commands import workbench_export

    workbench_export.export_pipeline(notebook, out, yaml_path, name)
