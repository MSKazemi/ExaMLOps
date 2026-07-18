"""``exa report`` — offline cost/carbon/SLA/project reports (Phase 3 item 3.5)."""

from __future__ import annotations

from datetime import UTC, datetime

import typer

from examlops.cli import _output

app = typer.Typer(no_args_is_help=True, help="Generate offline platform reports (cost/carbon/SLA)")

_EX = (
    "Examples:\n\n"
    "  exa report generate --out report.html\n\n"
    "  exa report generate --format text\n\n"
    "  exa report generate --format pdf --project research --out research.pdf"
)


@app.command("generate", epilog=_EX)
def generate(
    fmt: str = typer.Option("html", "--format", "-f", help="html | pdf | text"),
    out: str | None = typer.Option(None, "--out", "-o", help="Write to this file (else stdout)"),
    project: str | None = typer.Option(None, "--project", help="Scope to one project"),
) -> None:
    """Assemble + render a cost/carbon/project report. PDF degrades to HTML if WeasyPrint is absent."""
    from examlops import reporting

    result = reporting.generate(
        fmt, out=out, project=project, generated_at=datetime.now(UTC).isoformat(timespec="seconds")
    )
    if _output.json_mode:
        _output.print_json({k: v for k, v in result.items() if k != "content"})
        return
    if result.get("degraded"):
        _output.warning("WeasyPrint not installed — produced HTML instead of PDF.")
    if "path" in result:
        _output.ok(f"Report written to {result['path']} ({result['format']}).")
    else:
        _output.console.print(result["content"])
