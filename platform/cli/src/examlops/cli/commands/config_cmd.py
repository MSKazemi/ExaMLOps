from __future__ import annotations

import typer

from examlops.cli import _output
from examlops.cli._config import (
    config_path,
    list_contexts,
    load_config,
    set_active_context,
    write_config,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_SHOW = "Examples:\n\n  exa config show"
_EXAMPLES_INIT = "Examples:\n\n  exa config init"
_EXAMPLES_SET = (
    "Examples:\n\n"
    "  exa config set control_plane http://137.204.56.169:18002\n\n"
    "  exa config set control_plane_token mysecrettoken\n\n"
    "  exa config set mlflow http://137.204.56.169:15000"
)


@app.command(epilog=_EXAMPLES_SHOW)
def show():
    """Print the current resolved config (env vars + TOML file)."""
    cfg = load_config()
    data = {
        "control_plane_url": cfg.control_plane_url,
        "ray_serve_url": cfg.ray_serve_url,
        "mlflow_url": cfg.mlflow_url,
        "prefect_url": cfg.prefect_url,
        "dashboard_url": cfg.dashboard_url,
        "control_plane_token": "***" if cfg.control_plane_token else "(unset)",
        "config_file": str(config_path()),
    }
    _output.print_record(data)


@app.command(epilog=_EXAMPLES_INIT)
def init():
    """Interactive wizard — write ~/.config/examlops/config.toml."""
    cfg = load_config()
    typer.echo("Press Enter to keep current value shown in [brackets].\n")
    updates = {}
    for key, current in [
        ("control_plane", cfg.control_plane_url),
        ("ray_serve", cfg.ray_serve_url),
        ("mlflow", cfg.mlflow_url),
        ("prefect", cfg.prefect_url),
        ("dashboard", cfg.dashboard_url),
    ]:
        val = typer.prompt(f"  {key} URL", default=current)
        if val != current:
            updates[key] = val
    token = typer.prompt(
        "  control_plane_token", default=cfg.control_plane_token or "", hide_input=True
    )
    if token != cfg.control_plane_token:
        updates["control_plane_token"] = token
    if updates:
        write_config(updates)
        _output.ok(f"Config saved to {config_path()}")
    else:
        typer.echo("No changes.")


@app.command(name="set", epilog=_EXAMPLES_SET)
def set_config(
    key: str = typer.Argument(
        ..., help="Config key (e.g. control_plane, ray_serve, control_plane_token)"
    ),
    value: str = typer.Argument(..., help="New value"),
    context: str = typer.Option(
        "", "--context", "-c", help="Write into a named context instead of the default"
    ),
):
    """Set a single config key in ~/.config/examlops/config.toml."""
    write_config({key: value}, context=context or None)
    where = f" (context: {context})" if context else ""
    _output.ok(f"Set {key} = {value}{where}")


_EXAMPLES_CONTEXTS = "Examples:\n\n  exa config contexts\n\n  exa --json config contexts"
_EXAMPLES_USE = (
    "Examples:\n\n"
    "  [dim]# Point config at a named environment[/dim]\n"
    "  exa config use lxp\n\n"
    "  [dim]# Create + populate a context, then switch to it[/dim]\n"
    "  exa config set control_plane http://23.109.46.77:18002 --context lxp\n"
    "  exa config use lxp"
)


@app.command(epilog=_EXAMPLES_CONTEXTS)
def contexts():
    """List configured contexts (environments) and show the active one."""
    names, active = list_contexts()
    if _output.json_mode:
        _output.print_json({"contexts": names, "active": active})
        return
    if not names:
        _output.info(
            "No named contexts. Create one with: exa config set <key> <val> --context <name>"
        )
        return
    rows = [[("→ " if n == active else "  ") + n, "active" if n == active else ""] for n in names]
    _output.print_table("Contexts", ["Name", ""], rows)


@app.command(epilog=_EXAMPLES_USE)
def use(name: str = typer.Argument(..., help="Context name to activate")):
    """Switch the active context (environment)."""
    set_active_context(name)
    _output.ok(f"Active context is now [bold]{name}[/bold]")
    _output.hint("Verify effective settings with: exa env")
