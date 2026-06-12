"""exa modelzoo — ModelZoo repository freshness and integration commands."""

from __future__ import annotations

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_STATUS_LABEL = {"stale": "STALE", "current": "CURRENT", "unknown": "—"}

_EXAMPLES_STATUS = "Examples:\n\n  exa modelzoo status\n\n  exa --json modelzoo status"
_EXAMPLES_EVENTS = "Examples:\n\n  exa modelzoo events\n\n  exa modelzoo events --limit 20"
_EXAMPLES_SYNC = "Examples:\n\n  exa modelzoo sync"
_EXAMPLES_CONFIG = "Examples:\n\n  exa modelzoo config"


@app.command("status", epilog=_EXAMPLES_STATUS)
def status():
    """Show ModelZoo freshness for every registered model."""
    cfg = load_config()
    url = f"{cfg.control_plane_url}/modelzoo/status"
    try:
        data = _client.get(url)
    except _client.ClientError as e:
        _output.error(str(e))
        return

    models = data.get("models", [])
    last_event = data.get("last_event")

    if _output.json_mode:
        _output.print_json(models)
        return

    rows = []
    for m in models:
        label = _STATUS_LABEL.get(m["status"], "—")
        stale_since = (
            str(m["stale_since"] or "—")[:19].replace("T", " ") if m["stale_since"] else "—"
        )
        commit = (m["latest_modelzoo_commit"] or "—")[:8]
        rows.append([m["model_id"], label, stale_since, commit])

    _output.print_table(
        "ModelZoo Freshness",
        ["Model", "Status", "Stale Since", "Latest Commit"],
        rows,
    )
    if last_event:
        _output.console.print(
            f"[dim]Last push: {last_event['commit_sha'][:8]} "
            f"({last_event['source']}) at {str(last_event['timestamp'])[:19]}[/dim]"
        )


@app.command("events", epilog=_EXAMPLES_EVENTS)
def events(limit: int = typer.Option(10, "--limit", "-n", help="Number of events to show")):
    """Show recent ModelZoo push events."""
    cfg = load_config()
    url = f"{cfg.control_plane_url}/modelzoo/events?limit={limit}"
    try:
        data = _client.get(url)
    except _client.ClientError as e:
        _output.error(str(e))
        return

    if _output.json_mode:
        _output.print_json(data)
        return

    if not isinstance(data, list):
        _output.error("Unexpected response from server")
        return

    rows = [
        [
            str(e["id"]),
            (e["commit_sha"] or "")[:8],
            e.get("branch", "—"),
            e.get("pushed_by") or "—",
            str(e["timestamp"])[:19].replace("T", " "),
            e.get("source", "—"),
        ]
        for e in data
    ]
    _output.print_table(
        "ModelZoo Events",
        ["#", "Commit", "Branch", "Pushed By", "Timestamp", "Source"],
        rows,
    )


@app.command("sync", epilog=_EXAMPLES_SYNC)
def sync():
    """Manually trigger one ModelZoo poll cycle."""
    cfg = load_config()
    url = f"{cfg.control_plane_url}/modelzoo/sync"
    try:
        data = _client.post(url, {}, token=cfg.control_plane_token, timeout=30.0)
    except _client.ClientError as e:
        _output.error(str(e))
        return

    if _output.json_mode:
        _output.print_json(data)
        return

    if data.get("new_commit"):
        sha = (data.get("commit_sha") or "")[:8]
        n = data.get("models_marked_stale", 0)
        _output.ok(f"New commit {sha} detected — {n} model(s) marked stale")
    else:
        _output.ok("Already up-to-date — no new ModelZoo commits")


@app.command("config", epilog=_EXAMPLES_CONFIG)
def show_config():
    """Show ModelZoo integration configuration."""
    cfg = load_config()
    url = f"{cfg.control_plane_url}/modelzoo/config"
    try:
        data = _client.get(url)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    _output.print_record(data)


_EXAMPLES_CONFIG_SET = (
    "Examples:\n\n"
    "  exa modelzoo config-set auto_retrain true\n\n"
    "  exa modelzoo config-set poll_interval_seconds 120"
)


@app.command("config-set", epilog=_EXAMPLES_CONFIG_SET)
def set_config(
    key: str = typer.Argument(..., help="Config key: auto_retrain | poll_interval_seconds"),
    value: str = typer.Argument(..., help="New value"),
):
    """Update ModelZoo integration config on the Control Plane."""
    cfg = load_config()
    payload: dict = {}
    if key == "auto_retrain":
        payload["auto_retrain"] = value.lower() in ("true", "1", "yes")
    elif key == "poll_interval_seconds":
        try:
            payload["poll_interval_seconds"] = int(value)
        except ValueError:
            _output.error(f"poll_interval_seconds must be an integer, got: {value!r}")
            raise typer.Exit(1)
    else:
        _output.error(f"Unknown config key {key!r}. Supported: auto_retrain, poll_interval_seconds")
        raise typer.Exit(1)
    url = f"{cfg.control_plane_url}/modelzoo/config"
    try:
        data = _client.put(url, payload, token=cfg.control_plane_token)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    if _output.json_mode:
        _output.print_json(data)
        return
    _output.ok(f"Updated {key}={value}")
    _output.print_record(data)
