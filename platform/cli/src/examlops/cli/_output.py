from __future__ import annotations

import json
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

# Set by main.py callback flags
json_mode: bool = False  # True for any structured (non-table) format — gates existing branches
yes_mode: bool = False  # --yes/-y skips confirmation prompts
quiet_mode: bool = False  # --quiet/-q suppresses non-essential chatter (info/hint/detail)
verbose_mode: bool = False  # --verbose/-v enables extra detail() output
output_format: str = "table"  # one of: table | json | yaml | csv


# ── Core output primitives ────────────────────────────────────────────────────


def print_json(data: Any) -> None:
    """Emit structured data in the active machine-readable format (json/yaml/csv/md/html).

    Named ``print_json`` for historical reasons — it now dispatches on ``output_format``.
    The whole CLI reaches structured output through this one function, so every format is
    uniform without touching individual commands (add a format here, all commands gain it).
    """
    if output_format == "yaml":
        typer.echo(_to_yaml(data))
    elif output_format == "csv":
        typer.echo(_to_csv(data))
    elif output_format == "md":
        typer.echo(_to_md(data))
    elif output_format == "html":
        typer.echo(_to_html(data))
    else:
        typer.echo(json.dumps(data, indent=2, default=str))


def _to_yaml(data: Any) -> str:
    try:
        import yaml

        return yaml.safe_dump(
            data, sort_keys=False, default_flow_style=False, allow_unicode=True
        ).rstrip("\n")
    except Exception:
        # Fall back to JSON if PyYAML is unavailable or the data isn't representable.
        return json.dumps(data, indent=2, default=str)


def _to_csv(data: Any) -> str:
    import csv
    import io

    buf = io.StringIO()
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        fields: list[str] = []
        for row in data:
            for k in row:
                if k not in fields:
                    fields.append(k)
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in data:
            writer.writerow({k: _scalar(v) for k, v in row.items()})
    elif isinstance(data, dict):
        row_writer = csv.writer(buf)
        row_writer.writerow(["key", "value"])
        for k, v in data.items():
            row_writer.writerow([k, _scalar(v)])
    else:
        # Not tabular — degrade to JSON so no data is silently lost.
        return json.dumps(data, default=str)
    return buf.getvalue().rstrip("\n")


def _scalar(value: Any) -> Any:
    """Flatten nested values so CSV cells stay single-line."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def _md_cell(value: Any) -> str:
    """Render a value for a Markdown table cell — flattened and pipe-escaped."""
    return str(_scalar(value)).replace("|", "\\|").replace("\n", " ")


def _to_md(data: Any) -> str:
    """Render structured data as a GitHub-flavoured Markdown table.

    List-of-dicts → a column table; a flat dict → a two-column Key/Value table; anything
    else degrades to a fenced JSON block so no data is silently lost (mirrors CSV).
    """
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        fields: list[str] = []
        for row in data:
            for k in row:
                if k not in fields:
                    fields.append(k)
        lines = ["| " + " | ".join(fields) + " |", "| " + " | ".join(["---"] * len(fields)) + " |"]
        for row in data:
            lines.append("| " + " | ".join(_md_cell(row.get(k, "")) for k in fields) + " |")
        return "\n".join(lines)
    if isinstance(data, dict):
        lines = ["| Key | Value |", "| --- | --- |"]
        for k, v in data.items():
            lines.append(f"| {_md_cell(k)} | {_md_cell(v)} |")
        return "\n".join(lines)
    return f"```json\n{json.dumps(data, indent=2, default=str)}\n```"


def _to_html(data: Any) -> str:
    """Render structured data as a self-contained HTML ``<table>`` (values HTML-escaped).

    List-of-dicts → a column table; a flat dict → a two-column table; anything else degrades
    to an escaped ``<pre>`` JSON block.
    """
    import html as _html

    def esc(v: Any) -> str:
        return _html.escape(str(_scalar(v)))

    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        fields = []
        for row in data:
            for k in row:
                if k not in fields:
                    fields.append(k)
        head = "".join(f"<th>{esc(k)}</th>" for k in fields)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(row.get(k, ''))}</td>" for k in fields) + "</tr>"
            for row in data
        )
        return f"<table>\n<thead><tr>{head}</tr></thead>\n<tbody>{body}</tbody>\n</table>"
    if isinstance(data, dict):
        rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in data.items())
        return f"<table>\n<tbody>{rows}</tbody>\n</table>"
    return f"<pre>{esc(json.dumps(data, indent=2, default=str))}</pre>"


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
    """Informational message — skipped in JSON or quiet mode."""
    if json_mode or quiet_mode:
        return
    console.print(f"[dim]{message}[/dim]")


def hint(message: str) -> None:
    """Suggest a next action — skipped in JSON or quiet mode.

    The message is Rich-escaped so literal brackets (e.g. a TOML
    ``[project.entry-points."examlops.cli_plugins"]`` snippet) render verbatim
    instead of being silently eaten as an invalid markup tag.
    """
    if json_mode or quiet_mode:
        return
    console.print(f"[dim italic]  → {escape(message)}[/dim italic]")


def detail(message: str) -> None:
    """Extra diagnostic detail — shown only under --verbose (never in JSON/quiet)."""
    if json_mode or quiet_mode or not verbose_mode:
        return
    console.print(f"[dim]· {message}[/dim]")


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


# ── Live / watch ───────────────────────────────────────────────────────────


def watch_loop(render: Callable[[], None], interval: int) -> None:
    """Re-run ``render`` every ``interval`` seconds, clearing the screen between frames.

    Exits cleanly on Ctrl-C. No-op guard: callers should skip this in JSON mode.
    """
    import time

    interval = max(1, int(interval))
    try:
        while True:
            console.clear()
            console.print(
                f"[dim]● live — refreshing every {interval}s — press Ctrl-C to exit[/dim]"
            )
            render()
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watching.[/dim]")


# ── Visual primitives (graphics) ─────────────────────────────────────────────

_SPARK_TICKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float]) -> str:
    """Render a compact unicode sparkline from a numeric series (empty string if <2 points)."""
    nums = [float(v) for v in values if v is not None]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    span = hi - lo
    if span == 0:
        return _SPARK_TICKS[0] * len(nums)
    out = []
    last = len(_SPARK_TICKS) - 1
    for n in nums:
        idx = int((n - lo) / span * last)
        out.append(_SPARK_TICKS[min(last, max(0, idx))])
    return "".join(out)


def bar(value: float, maximum: float, width: int = 20) -> str:
    """Render a horizontal bar (``value`` of ``maximum``) using block characters."""
    if maximum <= 0:
        return "░" * width
    frac = max(0.0, min(1.0, value / maximum))
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)
