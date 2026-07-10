"""``exa plugins`` — list third-party CLI plugins discovered via entry points."""

from __future__ import annotations

from examlops.cli import _output, _plugins

_EXAMPLES = (
    "Examples:\n\n"
    "  exa plugins\n\n"
    "  exa --json plugins\n\n"
    "  [dim]# Register a plugin (in the plugin package's pyproject.toml):[/dim]\n"
    '  [dim][project.entry-points."examlops.cli_plugins"][/dim]\n'
    '  [dim]myteam = "my_pkg.cli:app"[/dim]'
)


def plugins() -> None:
    """List installed exa CLI plugins and whether each loaded successfully."""
    infos = _plugins.discover()
    if _output.json_mode:
        _output.print_json(
            [{"name": i.name, "target": i.value, "ok": i.ok, "error": i.error} for i in infos]
        )
        return
    if not infos:
        _output.info(f"No plugins installed (entry-point group: {_plugins.PLUGIN_GROUP}).")
        _output.hint('Add one via [project.entry-points."examlops.cli_plugins"] in a package.')
        return
    rows = [
        [i.name, i.value, "[green]✓[/green]" if i.ok else f"[red]✗[/red] {i.error}"] for i in infos
    ]
    _output.print_table("exa CLI plugins", ["Name", "Target", "Status"], rows)
