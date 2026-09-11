"""``exa connection`` — Named Connections (P2, ADR 0087).

Reusable, project-scoped data connections (S3 / URI / dataplane). Non-secret config is stored in
``platform.db``; credentials live in the D7 secrets client and are referenced, never copied.
"""

from __future__ import annotations

import json
import os

import typer

from examlops.cli import _output
from examlops.connections import (
    KINDS,
    ConnectionError,
    create_connection,
    delete_connection,
    get_connection,
    list_connections,
    test_connection,
)
from examlops.data.audit import write_audit_event

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Named Connections — reusable project-scoped data sources (S3/URI/dataplane).",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EX_CREATE = (
    "Examples:\n\n"
    "  exa connection create minio-data --kind s3 --project research \\\n"
    '      --config \'{"endpoint":"http://localhost:19000","bucket":"data","access_key":"minioadmin"}\' \\\n'
    "      --secret-value minioadmin\n\n"
    '  exa connection create zenodo --kind uri --config \'{"uri":"https://zenodo.org/record/123"}\''
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command(epilog=_EX_CREATE)
def create(
    name: str = typer.Argument(..., help="Connection name (unique within its project)"),
    kind: str = typer.Option(
        "s3",
        "--kind",
        "-k",
        help=(
            f"Connection kind: {', '.join(KINDS)}, or any dataplane connector kind "
            "(see: exa dataplane connectors)"
        ),
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help="Owning project (omit = global)"
    ),
    config: str = typer.Option("{}", "--config", "-c", help="Non-secret config as JSON"),
    secret_value: str | None = typer.Option(
        None, "--secret-value", help="Secret (stored in the secrets client, never in platform.db)"
    ),
) -> None:
    """Create a named connection."""
    try:
        cfg = json.loads(config)
    except json.JSONDecodeError as exc:
        _output.error(f"--config is not valid JSON: {exc}")
        raise typer.Exit(1) from exc
    if get_connection(name, project=project):
        _output.error(f"Connection '{name}' already exists (project={project or '-'})")
        raise typer.Exit(1)
    try:
        create_connection(
            name, kind, project=project, config=cfg, secret_value=secret_value, created_by=_actor()
        )
    except ConnectionError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    write_audit_event(
        "cli", _actor(), "connection_created", name, {"project": project, "kind": kind}
    )
    _output.ok(
        f"Connection '{name}' created ({kind})"
        + (f" in project '{project}'" if project else " (global)")
        + (" · secret stored" if secret_value else "")
    )


@app.command("list")
def connection_list(
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project"),
) -> None:
    """List connections (metadata only — never secret values)."""
    rows = list_connections(project=project)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.warning("No connections. Create one: exa connection create <name> --kind s3")
        return
    _output.print_table(
        "Connections",
        ["Name", "Project", "Kind", "Secret", "Config"],
        [
            [
                r["name"],
                r["project"] or "(global)",
                r["kind"],
                "yes" if r["has_secret"] else "-",
                ", ".join(f"{k}={v}" for k, v in r["config"].items() if "secret" not in k.lower()),
            ]
            for r in rows
        ],
    )


@app.command()
def show(
    name: str = typer.Argument(..., help="Connection name"),
    project: str | None = typer.Option(None, "--project", "-p", help="Owning project"),
) -> None:
    """Show one connection (config + secret presence, never the secret value)."""
    c = get_connection(name, project=project)
    if not c:
        _output.error(f"Connection '{name}' not found")
        raise typer.Exit(1)
    if _output.json_mode:
        _output.print_json(c)
        return
    _output.print_table(
        f"Connection: {name}",
        ["Field", "Value"],
        [
            ["Project", c["project"] or "(global)"],
            ["Kind", c["kind"]],
            ["Secret ref", c["secret_ref"] or "(none)"],
            ["Created at", c["created_at"]],
            ["Created by", c["created_by"] or ""],
            ["Config", json.dumps(c["config"])],
        ],
    )


@app.command()
def test(
    name: str = typer.Argument(..., help="Connection name"),
    project: str | None = typer.Option(None, "--project", "-p", help="Owning project"),
) -> None:
    """Read-only reachability probe (exit 1 on failure; never prints secrets)."""
    result = test_connection(name, project=project)
    if _output.json_mode:
        _output.print_json(result)
    elif result["ok"]:
        _output.ok(f"{name}: reachable — {result['detail']}")
    else:
        _output.error(f"{name}: {result['detail']}")
    if not result["ok"]:
        raise typer.Exit(1)


@app.command()
def delete(
    name: str = typer.Argument(..., help="Connection name"),
    project: str | None = typer.Option(None, "--project", "-p", help="Owning project"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a connection (the referenced secret is left intact)."""
    if not yes and not _output.confirm(f"Delete connection '{name}'?"):
        _output.info("Cancelled.")
        return
    if not delete_connection(name, project=project):
        _output.error(f"Connection '{name}' not found")
        raise typer.Exit(1)
    write_audit_event("cli", _actor(), "connection_deleted", name, {"project": project})
    _output.ok(f"Connection '{name}' deleted")
