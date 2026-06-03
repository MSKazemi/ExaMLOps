from __future__ import annotations

import urllib.parse

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config

_LOWER_IS_BETTER = {"rmse", "mae", "loss", "error", "mse", "mape"}


def _improvement_direction(metric: str) -> str:
    for kw in _LOWER_IS_BETTER:
        if kw in metric.lower():
            return "lower"
    return "higher"

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

_EXAMPLES_LIST = (
    "Examples:\n\n"
    "  exa models list\n\n"
    "  exa --json models list"
)
_EXAMPLES_INFO = (
    "Examples:\n\n"
    "  exa models info jpcp\n\n"
    "  exa --json models info jpcp"
)


@app.command("list", epilog=_EXAMPLES_LIST)
def list_models():
    """List all registered models with their production alias and latest version."""
    cfg = load_config()
    url = f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/registered-models/list"
    try:
        data = _client.get(url)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    models = data.get("registered_models", [])
    rows = []
    for m in models:
        aliases = {a["alias"]: a["version"] for a in m.get("aliases", [])}
        prod_ver = aliases.get("Production", "—")
        latest = max((v["version"] for v in m.get("latest_versions", [])), default="—")
        rows.append([m["name"], prod_ver, latest, ", ".join(aliases.keys()) or "—"])
    _output.print_table("Registered Models", ["Name", "Production", "Latest", "Aliases"], rows)


@app.command(epilog=_EXAMPLES_INFO)
def info(model: str = typer.Argument(..., help="Registered model name (e.g. jpcp)")):
    """Show detail for one model: all versions, aliases, metrics."""
    cfg = load_config()
    url = (f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/registered-models/get"
           f"?name={urllib.parse.quote(model)}")
    try:
        data = _client.get(url)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    rm = data.get("registered_model", {})
    if _output.json_mode:
        _output.print_json(rm)
        return
    _output.print_record({
        "name":     rm.get("name"),
        "aliases":  ", ".join(f"{a['alias']}=v{a['version']}" for a in rm.get("aliases", [])) or "none",
        "versions": ", ".join(v["version"] for v in rm.get("latest_versions", [])) or "none",
    })


_EXAMPLES_DIFF = (
    "Examples:\n\n"
    "  exa models diff jpcp 17 18\n\n"
    "  exa --json models diff jpcp 17 18"
)


@app.command(epilog=_EXAMPLES_DIFF)
def diff(
    model: str = typer.Argument(..., help="Registered model name (e.g. jpcp)"),
    v1: str = typer.Argument(..., help="First version number"),
    v2: str = typer.Argument(..., help="Second version number"),
):
    """Compare metrics and params between two model versions."""
    cfg = load_config()

    def _get_run(version: str) -> tuple[str, dict, dict]:
        ver_url = (
            f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/model-versions/get"
            f"?name={urllib.parse.quote(model)}&version={version}"
        )
        try:
            ver_data = _client.get(ver_url)
        except _client.ClientError as e:
            _output.error(str(e))
            raise  # re-raise ClientError so outer try/except catches it
        run_id = ver_data["model_version"]["run_id"]
        run_url = f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/runs/get?run_id={run_id}"
        try:
            run_data = _client.get(run_url)
        except _client.ClientError as e:
            _output.error(str(e))
            raise
        d = run_data["run"]["data"]
        return run_id, d.get("metrics", {}), d.get("params", {})

    try:
        _, metrics1, params1 = _get_run(v1)
        _, metrics2, params2 = _get_run(v2)
    except _client.ClientError:
        return

    all_metrics = sorted(set(metrics1) | set(metrics2))
    all_params = sorted(set(params1) | set(params2))

    if _output.json_mode:
        _output.print_json({
            "model": model,
            "v1": v1,
            "v2": v2,
            "metrics": {
                k: {"v1": metrics1.get(k), "v2": metrics2.get(k)}
                for k in all_metrics
            },
            "params": {
                k: {"v1": params1.get(k), "v2": params2.get(k)}
                for k in all_params
            },
        })
        return

    from rich.table import Table
    from examlops.cli._output import console
    table = Table(title=f"{model}  v{v1} → v{v2}", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column(f"v{v1}")
    table.add_column(f"v{v2}")
    table.add_column("Δ")

    for k in all_metrics:
        val1 = metrics1.get(k)
        val2 = metrics2.get(k)
        if val1 is not None and val2 is not None:
            delta = val2 - val1
            direction = _improvement_direction(k)
            improved = (delta < 0 and direction == "lower") or (delta > 0 and direction == "higher")
            delta_str = f"[green]{delta:+.4f}[/green]" if improved else f"[red]{delta:+.4f}[/red]"
        else:
            delta_str = "—"
        table.add_row(
            k,
            f"{val1:.4f}" if val1 is not None else "—",
            f"{val2:.4f}" if val2 is not None else "—",
            delta_str,
        )

    for k in all_params:
        val1 = params1.get(k, "—")
        val2 = params2.get(k, "—")
        changed = val1 != val2
        p2_str = f"[yellow]{val2}[/yellow]" if changed else str(val2)
        table.add_row(k, str(val1), p2_str, "changed" if changed else "—")

    console.print(table)


_EXAMPLES_LINEAGE = (
    "Examples:\n\n"
    "  exa models lineage jpcp\n\n"
    "  exa models lineage jpcp 18\n\n"
    "  exa --json models lineage jpcp"
)


@app.command(epilog=_EXAMPLES_LINEAGE)
def lineage(
    model: str = typer.Argument(..., help="Registered model name (e.g. jpcp)"),
    version: str | None = typer.Argument(None, help="Version number (default: Production alias)"),
):
    """Show the pipeline → dataset → model version lineage chain."""
    cfg = load_config()

    if version is None:
        try:
            rm_data = _client.get(
                f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/registered-models/get"
                f"?name={urllib.parse.quote(model)}"
            )
        except _client.ClientError as e:
            _output.error(str(e))
            return
        aliases = {
            a["alias"]: a["version"]
            for a in rm_data.get("registered_model", {}).get("aliases", [])
        }
        version = aliases.get("Production") or next(iter(aliases.values()), None)
        if not version:
            _output.error(f"No versions found for {model}")
            return

    try:
        ver_data = _client.get(
            f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/model-versions/get"
            f"?name={urllib.parse.quote(model)}&version={version}"
        )
    except _client.ClientError as e:
        _output.error(str(e))
        return

    mv = ver_data.get("model_version", {})
    run_id = mv.get("run_id", "unknown")
    created_ms = mv.get("creation_timestamp", 0)

    try:
        run_data = _client.get(
            f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/runs/get?run_id={run_id}"
        )
    except _client.ClientError as e:
        _output.error(str(e))
        return

    run = run_data.get("run", {}).get("data", {})
    tags = {t["key"]: t["value"] for t in run.get("tags", [])}
    params = run.get("params", {})
    metrics = run.get("metrics", {})

    prefect_run = tags.get("prefect_flow_run_id", "unknown")
    dataset_version = tags.get("dataset_version", "unknown")
    training_rows = tags.get("training_rows", "unknown")

    if _output.json_mode:
        _output.print_json({
            "model": model,
            "model_version": version,
            "run_id": run_id,
            "created_ms": created_ms,
            "prefect_flow_run_id": prefect_run,
            "dataset_version": dataset_version,
            "training_rows": training_rows,
            "params": params,
            "metrics": metrics,
        })
        return

    from examlops.cli._output import console
    console.print(f"\n[bold cyan]Lineage — {model} v{version}[/bold cyan]")
    console.print(f"  [bold]Pipeline run[/bold]    {prefect_run}")
    console.print(f"  [bold]Dataset version[/bold] {dataset_version}  (rows: {training_rows})")
    console.print(f"  [bold]MLflow run[/bold]      {run_id}")
    console.print(f"  [bold]Model version[/bold]   {model} v{version}  (created_ms: {created_ms})")
    if metrics:
        metric_str = "  ".join(
            f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in metrics.items()
        )
        console.print(f"  [bold]Metrics[/bold]         {metric_str}")
