"""``exa env`` — show the effective CLI configuration and where each value comes from."""

from __future__ import annotations

import typer

from examlops.cli import _output
from examlops.cli._config import active_context, config_path, resolve_with_provenance

_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# Show effective settings + their source (env / context / file / default)[/dim]\n"
    "  exa env\n\n"
    "  [dim]# Machine-readable[/dim]\n"
    "  exa --json env"
)


def env(
    validate: bool = typer.Option(
        False, "--validate", help="Check the effective config for coherence; exit 1 on errors (4.2)"
    ),
) -> None:
    """Show the effective configuration and the source of every value.

    With ``--validate``, cross-check the effective environment for enterprise-readiness (backend ↔
    endpoint consistency, OIDC coherence, weak/placeholder secrets) and exit non-zero on any error.
    """
    if validate:
        import os

        from examlops.config_validate import has_errors
        from examlops.config_validate import validate as _validate

        findings = _validate(os.environ)
        if _output.json_mode:
            _output.print_json([f.as_dict() for f in findings])
        else:
            _output.print_table(
                "Config validation",
                ["Level", "Key", "Message"],
                [[f.level.upper(), f.key, f.message] for f in findings],
            )
        if has_errors(findings):
            raise typer.Exit(1)
        return

    rows = resolve_with_provenance()
    ctx = active_context()
    if _output.json_mode:
        _output.print_json(
            {
                "active_context": ctx,
                "config_file": str(config_path()),
                "settings": rows,
            }
        )
        return
    if ctx:
        _output.console.print(f"  [bold cyan]active context:[/bold cyan] {ctx}")
    _output.console.print(f"  [dim]config file: {config_path()}[/dim]\n")
    _output.print_table(
        "Effective configuration",
        ["Key", "Value", "Source"],
        [[r["key"], r["value"], r["source"]] for r in rows],
    )
    _output.hint("Switch environments with: exa config use <context>")
