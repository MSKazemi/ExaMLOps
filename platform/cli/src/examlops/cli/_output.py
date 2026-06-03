from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

# Set by main.py --json callback
json_mode: bool = False


def print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def error(message: str, exit_code: int = 1) -> None:
    if json_mode:
        print_json({"error": message, "exit_code": exit_code})
    else:
        err_console.print(f"[red][error][/red] {message}")
    raise typer.Exit(exit_code)


def print_table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    if json_mode:
        print_json([dict(zip(columns, row)) for row in rows])
        return
    table = Table(title=title, show_header=True, header_style="bold cyan")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(v) if v is not None else "—" for v in row])
    console.print(table)


def print_record(data: dict[str, Any]) -> None:
    if json_mode:
        print_json(data)
        return
    for k, v in data.items():
        console.print(f"[bold]{k}:[/bold] {v}")


def ok(message: str) -> None:
    if json_mode:
        print_json({"ok": True, "message": message})
    else:
        console.print(f"[green]✓[/green] {message}")
