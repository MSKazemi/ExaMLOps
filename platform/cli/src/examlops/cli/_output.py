from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

# Set by main.py callback flags
json_mode: bool = False
yes_mode: bool = False  # --yes/-y skips confirmation prompts


# ── Core output primitives ────────────────────────────────────────────────────


def print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def ok(message: str) -> None:
    if json_mode:
        print_json({"ok": True, "message": message})
    else:
        console.print(f"[green]✓[/green] {message}")


def error(message: str, exit_code: int = 1, hint: str | None = None) -> None:
    if json_mode:
        payload: dict[str, Any] = {"error": message, "exit_code": exit_code}
        if hint:
            payload["hint"] = hint
        print_json(payload)
    else:
        err_console.print(f"[red]✗[/red] {message}")
        if hint:
            err_console.print(f"[dim]  → {hint}[/dim]")
    raise typer.Exit(exit_code)


def warning(message: str) -> None:
    """Non-fatal warning — does not exit."""
    if json_mode:
        print_json({"warning": message})
    else:
        err_console.print(f"[yellow]⚠[/yellow] {message}")


def info(message: str) -> None:
    """Informational message — skipped in JSON mode."""
    if json_mode:
        return
    console.print(f"[dim]{message}[/dim]")


def hint(message: str) -> None:
    """Suggest a next action — skipped in JSON mode."""
    if json_mode:
        return
    console.print(f"[dim italic]  → {message}[/dim italic]")


# ── Structured output ─────────────────────────────────────────────────────────


def print_table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    if json_mode:
        print_json([dict(zip(columns, row)) for row in rows])
        return
    n = len(rows)
    caption = f"{n} item{'s' if n != 1 else ''}"
    table = Table(title=title, show_header=True, header_style="bold cyan", caption=caption)
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
        console.print(f"  [bold cyan]{k}:[/bold cyan] {v}")


# ── Interaction helpers ───────────────────────────────────────────────────────


def confirm(prompt: str, default: bool = False) -> bool:
    """Prompt for confirmation — returns True immediately when --yes or --json."""
    if yes_mode or json_mode:
        return True
    return typer.confirm(prompt, default=default)


# ── Progress ─────────────────────────────────────────────────────────────────


@contextmanager
def spinner(message: str) -> Generator[None, None, None]:
    """Display a Rich spinner while the block executes. No-op in JSON mode."""
    if json_mode:
        yield
        return
    with console.status(f"[bold blue]{message}[/bold blue]"):
        yield
