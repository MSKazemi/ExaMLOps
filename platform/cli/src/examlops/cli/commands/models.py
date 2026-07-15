from __future__ import annotations

import os
import re
import subprocess
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


app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_LIST = "Examples:\n\n  exa models list\n\n  exa --json models list"
_EXAMPLES_INFO = "Examples:\n\n  exa models info jpcp\n\n  exa --json models info jpcp"


@app.command("list", epilog=_EXAMPLES_LIST)
def list_models():
    """List all registered models with their production alias and latest version."""
    cfg = load_config()
    url = f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/search"
    try:
        data = _client.get(url)
    except _client.ClientError as e:
        _output.error(f"Failed to list models: {e}", hint="Is MLflow running? Try: exa status")
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
    url = f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get?name={urllib.parse.quote(model)}"
    try:
        data = _client.get(url)
    except _client.ClientError as e:
        _output.error(
            f"Failed to fetch model {model!r}: {e}", hint="Check model name with: exa models list"
        )
        return
    rm = data.get("registered_model", {})
    if _output.json_mode:
        _output.print_json(rm)
        return
    _output.print_record(
        {
            "name": rm.get("name"),
            "aliases": ", ".join(f"{a['alias']}=v{a['version']}" for a in rm.get("aliases", []))
            or "none",
            "versions": ", ".join(v["version"] for v in rm.get("latest_versions", [])) or "none",
        }
    )


_EXAMPLES_DIFF = "Examples:\n\n  exa models diff jpcp 17 18\n\n  exa --json models diff jpcp 17 18"


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
            f"{cfg.mlflow_url}/api/2.0/mlflow/model-versions/get"
            f"?name={urllib.parse.quote(model)}&version={version}"
        )
        try:
            ver_data = _client.get(ver_url)
        except _client.ClientError as e:
            _output.error(str(e))
            raise  # re-raise ClientError so outer try/except catches it
        run_id = ver_data["model_version"]["run_id"]
        run_url = f"{cfg.mlflow_url}/api/2.0/mlflow/runs/get?run_id={run_id}"
        try:
            run_data = _client.get(run_url)
        except _client.ClientError as e:
            _output.error(str(e))
            raise
        d = run_data["run"]["data"]
        metrics = {m["key"]: m["value"] for m in d.get("metrics", [])}
        params = {p["key"]: p["value"] for p in d.get("params", [])}
        return run_id, metrics, params

    try:
        _, metrics1, params1 = _get_run(v1)
        _, metrics2, params2 = _get_run(v2)
    except _client.ClientError:
        return

    all_metrics = sorted(set(metrics1) | set(metrics2))
    all_params = sorted(set(params1) | set(params2))

    if _output.json_mode:
        _output.print_json(
            {
                "model": model,
                "v1": v1,
                "v2": v2,
                "metrics": {k: {"v1": metrics1.get(k), "v2": metrics2.get(k)} for k in all_metrics},
                "params": {k: {"v1": params1.get(k), "v2": params2.get(k)} for k in all_params},
            }
        )
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
                f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get"
                f"?name={urllib.parse.quote(model)}"
            )
        except _client.ClientError as e:
            _output.error(str(e))
            return
        aliases = {
            a["alias"]: a["version"] for a in rm_data.get("registered_model", {}).get("aliases", [])
        }
        version = aliases.get("Production") or next(iter(aliases.values()), None)
        if not version:
            _output.error(f"No versions found for {model}")
            return

    try:
        ver_data = _client.get(
            f"{cfg.mlflow_url}/api/2.0/mlflow/model-versions/get"
            f"?name={urllib.parse.quote(model)}&version={version}"
        )
    except _client.ClientError as e:
        _output.error(str(e))
        return

    mv = ver_data.get("model_version", {})
    run_id = mv.get("run_id", "unknown")
    created_ms = mv.get("creation_timestamp", 0)

    try:
        run_data = _client.get(f"{cfg.mlflow_url}/api/2.0/mlflow/runs/get?run_id={run_id}")
    except _client.ClientError as e:
        _output.error(str(e))
        return

    run = run_data.get("run", {}).get("data", {})
    tags = {t["key"]: t["value"] for t in run.get("tags", [])}
    params = {p["key"]: p["value"] for p in run.get("params", [])}
    metrics = {m["key"]: m["value"] for m in run.get("metrics", [])}

    prefect_run = tags.get("prefect_flow_run_id", "unknown")
    dataset_version = tags.get("dataset_version", "unknown")
    training_rows = tags.get("training_rows", "unknown")

    if _output.json_mode:
        _output.print_json(
            {
                "model": model,
                "model_version": version,
                "run_id": run_id,
                "created_ms": created_ms,
                "prefect_flow_run_id": prefect_run,
                "dataset_version": dataset_version,
                "training_rows": training_rows,
                "params": params,
                "metrics": metrics,
            }
        )
        return

    from examlops.cli._output import console

    console.print(f"\n[bold cyan]Lineage — {model} v{version}[/bold cyan]")
    console.print(f"  [bold]Pipeline run[/bold]    {prefect_run}")
    console.print(f"  [bold]Dataset version[/bold] {dataset_version}  (rows: {training_rows})")
    console.print(f"  [bold]MLflow run[/bold]      {run_id}")
    console.print(f"  [bold]Model version[/bold]   {model} v{version}  (created_ms: {created_ms})")
    if metrics:
        metric_str = "  ".join(
            f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in metrics.items()
        )
        console.print(f"  [bold]Metrics[/bold]         {metric_str}")


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

_GPU_COST_PER_HOUR_DEFAULT = 2.50
_CPU_COST_PER_HOUR_DEFAULT = 0.05

_EXAMPLES_COST = (
    "Examples:\n\n"
    "  exa models cost JPCP\n\n"
    "  exa models cost JPCP --record\n\n"
    "  exa --json models cost JPCP"
)


def _mock_slurm_data(model: str, version: int) -> tuple[str, float]:
    """Return a synthetic (job_id, gpu_hours) pair deterministic on model+version."""
    import hashlib

    seed = int(hashlib.md5(f"{model}{version}".encode()).hexdigest(), 16)
    # gpu_hours in [4.0, 24.0], two decimal places
    gpu_hours = round(4.0 + (seed % 2000) / 100.0, 2)
    job_id = f"job-{(seed % 90000) + 10000}"
    return job_id, gpu_hours


def _real_sacct(job_id: str) -> float | None:
    """Query sacct for a given job ID; return gpu_hours or None on failure."""
    try:
        out = subprocess.check_output(
            [
                "sacct",
                "-j",
                job_id,
                "--format=JobID,Elapsed,AllocTRES",
                "--noheader",
                "--parsable2",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None

    # AllocTRES may contain gres/gpu=N
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        elapsed_str = parts[1].strip()  # e.g. "02:30:00" or "1-02:30:00"
        alloc_tres = parts[2].strip()  # e.g. "cpu=16,mem=64G,gres/gpu=2"

        gpu_match = re.search(r"gres/gpu=(\d+)", alloc_tres)
        if gpu_match is None:
            continue
        n_gpus = int(gpu_match.group(1))

        # Parse elapsed: [[D-]HH:]MM:SS
        elapsed_hours = 0.0
        time_match = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+)", elapsed_str)
        if time_match:
            days = int(time_match.group(1) or 0)
            hrs = int(time_match.group(2))
            mins = int(time_match.group(3))
            secs = int(time_match.group(4))
            elapsed_hours = days * 24 + hrs + mins / 60 + secs / 3600

        return round(n_gpus * elapsed_hours, 4)
    return None


def _count_idset(idset: str) -> int:
    """Count ids in a Flux idset like '0-15' or '0-3,8,10-11'."""
    total = 0
    for part in (idset or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                total += int(hi) - int(lo) + 1
            except ValueError:
                continue
        else:
            total += 1
    return total


def _flux_elapsed_hours(eventlog: str) -> float:
    """Elapsed wall-hours between the first 'alloc' and the 'finish'/'free' event."""
    start = end = None
    for line in eventlog.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        name = parts[1]
        if name in ("alloc", "start") and start is None:
            start = ts
        if name in ("finish", "free", "clean"):
            end = ts
    if start is None or end is None or end < start:
        return 0.0
    return (end - start) / 3600.0


def _real_flux_cost(job_id: str) -> tuple[float, float] | None:
    """Query Flux for a job's (gpu_hours, cpu_hours), or None on failure.

    Runs ``flux`` locally (symmetric with ``_real_sacct``); use from a host where the
    Flux instance is reachable (e.g. the login node).
    """
    import json

    try:
        r_json = subprocess.check_output(
            ["flux", "job", "info", job_id, "R"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        eventlog = subprocess.check_output(
            ["flux", "job", "info", job_id, "eventlog"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None

    try:
        resource_set = json.loads(r_json)
    except json.JSONDecodeError:
        return None

    n_cpus = n_gpus = 0
    for entry in resource_set.get("execution", {}).get("R_lite", []):
        children = entry.get("children", {})
        n_cpus += _count_idset(children.get("core", ""))
        n_gpus += _count_idset(children.get("gpu", ""))

    elapsed_hours = _flux_elapsed_hours(eventlog)
    return round(n_gpus * elapsed_hours, 4), round(n_cpus * elapsed_hours, 4)


def _tag_mlflow_version(
    cfg,
    model: str,
    version: str,
    gpu_hours: float,
    cost_usd: float,
) -> None:
    """Set gpu_hours and cost_usd tags on an MLflow model version (best-effort)."""
    url = f"{cfg.mlflow_url}/api/2.0/mlflow/model-versions/set-tag"
    for key, value in [("gpu_hours", f"{gpu_hours:.4f}"), ("cost_usd", f"{cost_usd:.4f}")]:
        try:
            _client.post(url, {"name": model, "version": version, "key": key, "value": value})
        except _client.ClientError:
            pass  # tagging is best-effort; don't fail the command


@app.command(epilog=_EXAMPLES_COST)
def cost(
    model: str = typer.Argument(..., help="Registered model name (e.g. JPCP)"),
    record: bool = typer.Option(
        False,
        "--record",
        help="Fetch latest scheduler data (Slurm/Flux), record to DB and tag MLflow",
    ),
):
    """Show HPC cost history for a model.  Use --record to ingest new data."""
    from examlops.platform_db import get_model_costs, init_db, record_model_cost

    init_db()

    if record:
        cfg = load_config()

        # Resolve all versions for this model from MLflow
        try:
            rm_data = _client.get(
                f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get"
                f"?name={urllib.parse.quote(model)}"
            )
        except _client.ClientError as e:
            _output.error(str(e))
            return

        versions = rm_data.get("registered_model", {}).get("latest_versions", [])
        if not versions:
            _output.error(f"No versions found for model '{model}' in MLflow")
            return

        # Scheduler resolution mirrors the pipeline: EXAMLOPS_HPC_SCHEDULER wins,
        # EXAMLOPS_SLURM_MODE is the legacy fallback.
        default_scheduler = (
            os.getenv("EXAMLOPS_HPC_SCHEDULER")
            or ("slurm" if os.getenv("EXAMLOPS_SLURM_MODE", "mock") == "slurm" else "mock")
        ).lower()
        # Cost is computed by the pluggable 'cost' provider (ADR 0074) — the default 'flat-rate'
        # reproduces the original gpu_hours×rate(+cpu_hours×rate) using the same env defaults, and a
        # site can swap the rate card via [finops.cost] config / EXAMLOPS_COST_PROVIDER without code.
        from examlops.finops.cost import estimate_cost_via_provider

        recorded_count = 0

        for ver in versions:
            ver_num = int(ver["version"])
            run_id = ver.get("run_id")

            job_id = None
            scheduler = default_scheduler
            if run_id:
                try:
                    run_data = _client.get(
                        f"{cfg.mlflow_url}/api/2.0/mlflow/runs/get?run_id={run_id}"
                    )
                    tags = {
                        t["key"]: t["value"]
                        for t in run_data.get("run", {}).get("data", {}).get("tags", [])
                    }
                    job_id = tags.get("hpc_job_id") or tags.get("slurm_job_id")
                    scheduler = (tags.get("hpc_scheduler") or default_scheduler).lower()
                except _client.ClientError:
                    pass

            gpu_hours = None
            cost_usd = None
            if scheduler == "mock":
                job_id, gpu_hours = _mock_slurm_data(model, ver_num)
                cost_usd = estimate_cost_via_provider(gpu_hours)["cost_usd"]
            elif scheduler == "flux" and job_id:
                flux_cost = _real_flux_cost(job_id)
                if flux_cost is not None:
                    gpu_hours, cpu_hours = flux_cost
                    cost_usd = estimate_cost_via_provider(gpu_hours, cpu_hours)["cost_usd"]
            elif job_id:  # slurm (or any sacct-backed scheduler)
                gpu_hours = _real_sacct(job_id)
                if gpu_hours is not None:
                    cost_usd = estimate_cost_via_provider(gpu_hours)["cost_usd"]

            record_model_cost(model, ver_num, run_id, job_id, gpu_hours, cost_usd)

            if gpu_hours is not None and cost_usd is not None:
                _tag_mlflow_version(cfg, model, str(ver_num), gpu_hours, cost_usd)

            recorded_count += 1

        _output.ok(f"Recorded cost data for {recorded_count} version(s) of {model}")

    # Display cost history
    rows_data = get_model_costs(model)
    if not rows_data:
        if not record:
            _output.console.print(
                f"[yellow]No cost data for {model}. Run with --record to ingest.[/yellow]"
            )
        return

    rows = []
    for r in rows_data:
        run_short = (r["run_id"] or "—")[:8]
        gpu_str = f"{r['gpu_hours']:.2f}" if r["gpu_hours"] is not None else "—"
        cost_str = f"${r['cost_usd']:.2f}" if r["cost_usd"] is not None else "—"
        rec_date = (r["recorded_at"] or "—")[:10]
        rows.append([r["version"], run_short, r["job_id"] or "—", gpu_str, cost_str, rec_date])

    _output.print_table(
        f"Model Cost History: {model}",
        ["Version", "Run ID", "Job ID", "GPU Hours", "Cost (USD)", "Recorded"],
        rows,
    )

    # Visual trend of GPU-hours across versions (oldest → newest).
    gpu_series = [r["gpu_hours"] for r in rows_data if r["gpu_hours"] is not None]
    spark = _output.sparkline(gpu_series)
    if spark:
        _output.console.print(f"  [dim]GPU-hours trend:[/dim] [cyan]{spark}[/cyan]")


_EXAMPLES_COST_LIST = "Examples:\n\n  exa models cost-list\n\n  exa --json models cost-list"


@app.command("cost-list", epilog=_EXAMPLES_COST_LIST)
def cost_list():
    """Show HPC cost summary across all models."""
    from examlops.platform_db import get_db as _gdb
    from examlops.platform_db import init_db as _init

    _init()
    with _gdb() as conn:
        rows = conn.execute(
            "SELECT model_name, COUNT(*) as n_runs, "
            "SUM(gpu_hours) as total_gpu_hours, SUM(cost_usd) as total_cost_usd "
            "FROM model_costs GROUP BY model_name ORDER BY model_name"
        ).fetchall()
    if not rows:
        _output.ok("No cost data recorded. Run: exa models cost <MODEL> --record")
        return
    data = [
        {
            "model_name": r["model_name"],
            "n_runs": r["n_runs"],
            "total_gpu_hours": r["total_gpu_hours"],
            "total_cost_usd": r["total_cost_usd"],
        }
        for r in rows
    ]
    if _output.json_mode:
        _output.print_json(data)
        return
    table_rows = [
        [
            r["model_name"],
            str(r["n_runs"]),
            f"{r['total_gpu_hours']:.2f}" if r["total_gpu_hours"] else "—",
            f"${r['total_cost_usd']:.2f}" if r["total_cost_usd"] else "—",
        ]
        for r in data
    ]
    _output.print_table(
        "Model Cost Summary", ["Model", "Runs", "Total GPU-Hours", "Total Cost (USD)"], table_rows
    )
