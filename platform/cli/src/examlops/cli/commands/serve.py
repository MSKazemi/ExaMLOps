from __future__ import annotations

import os
import subprocess
import sys

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.platform_db import get_traffic_rules, init_db, set_traffic_rules, write_audit_event

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

_EXAMPLES_RELOAD = (
    "Examples:\n\n"
    "  # Hot-reload every Production model from MLflow into Ray Serve\n"
    "  exa serve reload\n\n"
    "  # Reload one model after promotion\n"
    "  exa serve reload --model JPCP"
)
_EXAMPLES_CHECK = (
    "Examples:\n\n"
    "  # Verify Ray Serve health and loaded model aliases\n"
    "  exa serve check"
)
_EXAMPLES_INFER_CHECK = (
    "Examples:\n\n"
    "  # POST one valid synthetic HPC job to /infer-pipeline/infer\n"
    "  exa serve infer-check"
)
_EXAMPLES_BENCHMARK = (
    "Examples:\n\n"
    "  exa serve benchmark\n\n"
    "  exa serve benchmark --requests 25"
)


@app.command(epilog=_EXAMPLES_RELOAD)
def reload(model: str | None = typer.Option(None, "--model", "-m", help="Reload one model (default: all)")):
    """Hot-reload Production models from MLflow into Ray Serve."""
    cfg = load_config()
    url = f"{cfg.ray_serve_url}/reload/{model}" if model else f"{cfg.ray_serve_url}/reload"
    try:
        result = _client.post(url, {})
    except _client.ClientError as e:
        _output.error(str(e))
        return
    count = result.get("count", "?")
    _output.ok(f"Reloaded {count} model(s)")
    if _output.json_mode:
        _output.print_json(result)


@app.command(epilog=_EXAMPLES_CHECK)
def check():
    """Smoke test Ray Serve: health check + one prediction per model."""
    cfg = load_config()
    try:
        health = _client.get(f"{cfg.ray_serve_url}/health")
    except _client.ClientError as e:
        _output.error(str(e))
        return
    _output.print_record(health)


@app.command("infer-check", epilog=_EXAMPLES_INFER_CHECK)
def infer_check():
    """Smoke-test the Ray Serve inference pipeline with a valid synthetic HPC job."""
    cfg = load_config()
    body = {
        "job_id": "smoke",
        "model_name": "JPCP",
        "alias": "Production",
        "embedding": [0.1] * 384,
        "num_nodes": 4,
        "user_id": "smoke",
    }
    try:
        result = _client.post(f"{cfg.ray_serve_url}/infer-pipeline/infer", body)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    _output.print_record(result)


_EXAMPLES_TRAFFIC = (
    "Examples:\n\n"
    "  # Show current split for JPCP\n"
    "  exa serve traffic JPCP\n\n"
    "  # Route 90% Production, 10% Canary\n"
    "  exa serve traffic JPCP --production 90 --canary 10\n\n"
    "  # Reset to 100% Production\n"
    "  exa serve traffic JPCP --production 100"
)


@app.command(epilog=_EXAMPLES_TRAFFIC)
def traffic(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    production: int | None = typer.Option(None, "--production", help="% traffic to Production alias"),
    canary: int | None = typer.Option(None, "--canary", help="% traffic to Canary alias"),
    staging: int | None = typer.Option(None, "--staging", help="% traffic to Staging alias"),
):
    """Show or set traffic split across model aliases (must sum to 100)."""
    init_db()

    if production is None and canary is None and staging is None:
        rules = get_traffic_rules(model)
        if rules is None:
            _output.ok(f"No traffic rules for {model} — 100% Production (default)")
            return
        if _output.json_mode:
            _output.print_json(rules)
        else:
            rows = [[alias, f"{pct}%"] for alias, pct in rules.items()]
            _output.print_table(f"Traffic split — {model}", ["Alias", "Weight"], rows)
        return

    rules: dict[str, int] = {}
    if production is not None:
        rules["Production"] = production
    if canary is not None:
        rules["Canary"] = canary
    if staging is not None:
        rules["Staging"] = staging

    total = sum(rules.values())
    if total != 100:
        _output.error(f"Weights must sum to 100, got {total}")
        raise typer.Exit(1)

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    set_traffic_rules(model, rules, actor)
    write_audit_event("cli", actor, "traffic_changed", model, rules)

    cfg = load_config()
    try:
        _client.post(f"{cfg.ray_serve_url}/traffic-rules/{model}", rules)
    except _client.ClientError:
        pass  # non-fatal: rules persisted in DB

    _output.ok(f"Traffic rules updated for {model}: {rules}")


@app.command(epilog=_EXAMPLES_BENCHMARK)
def benchmark(requests: int = typer.Option(200, "--requests", "-n", help="Number of benchmark requests")):
    """Benchmark Ray Serve using the dummy client and report latency stats."""
    cmd = [sys.executable, "platform/clients/dummy_client.py", "--benchmark", str(requests)]
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=False)  # noqa: S603
    except FileNotFoundError:
        _output.error("dummy_client.py not found — run exa serve benchmark from the repo root")
    except subprocess.CalledProcessError as e:
        _output.error(f"dummy_client.py exited with code {e.returncode}")
