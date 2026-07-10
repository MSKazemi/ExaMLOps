"""``exa env`` — show the effective CLI configuration and where each value comes from."""

from __future__ import annotations

from examlops.cli import _output
from examlops.cli._config import CONFIG_PATH, active_context, resolve_with_provenance

_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# Show effective settings + their source (env / context / file / default)[/dim]\n"
    "  exa env\n\n"
    "  [dim]# Machine-readable[/dim]\n"
    "  exa --json env"
)


def env() -> None:
    """Show the effective configuration and the source of every value."""
    rows = resolve_with_provenance()
    ctx = active_context()
    if _output.json_mode:
        _output.print_json(
            {
                "active_context": ctx,
                "config_file": str(CONFIG_PATH),
                "settings": rows,
            }
        )
        return
    if ctx:
        _output.console.print(f"  [bold cyan]active context:[/bold cyan] {ctx}")
    _output.console.print(f"  [dim]config file: {CONFIG_PATH}[/dim]\n")
    _output.print_table(
        "Effective configuration",
        ["Key", "Value", "Source"],
        [[r["key"], r["value"], r["source"]] for r in rows],
    )
    _output.hint("Switch environments with: exa config use <context>")
