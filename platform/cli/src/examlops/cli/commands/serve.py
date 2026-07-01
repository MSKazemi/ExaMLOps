from __future__ import annotations

import os
import subprocess
import sys

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli.commands import explain_cmd
from examlops.platform_db import get_traffic_rules, init_db, set_traffic_rules, write_audit_event

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(explain_cmd.app, name="explain", help="Feature importance explanations (XAI)")

_EXAMPLES_RELOAD = (
    "Examples:\n\n"
    "  # Hot-reload every Production model from MLflow into Ray Serve\n"
    "  exa serve reload\n\n"
    "  # Reload one model after promotion\n"
    "  exa serve reload --model JPCP"
)
_EXAMPLES_CHECK = (
    "Examples:\n\n  # Verify Ray Serve health and loaded model aliases\n  exa serve check"
)
_EXAMPLES_INFER_CHECK = (
    "Examples:\n\n"
    "  # POST one valid synthetic HPC job to /infer-pipeline/infer\n"
    "  exa serve infer-check"
)
_EXAMPLES_BENCHMARK = "Examples:\n\n  exa serve benchmark\n\n  exa serve benchmark --requests 25"


@app.command(epilog=_EXAMPLES_RELOAD)
def reload(
    model: str | None = typer.Option(None, "--model", "-m", help="Reload one model (default: all)"),
) -> None:
    """Hot-reload Production models from MLflow into Ray Serve."""
    cfg = load_config()
    url = f"{cfg.ray_serve_url}/reload/{model}" if model else f"{cfg.ray_serve_url}/reload"
    target = model or "all models"
    with _output.spinner(f"Reloading {target} from MLflow into Ray Serve…"):
        try:
            result = _client.post(url, {})
        except _client.ClientError as e:
            _output.error(
                f"Failed to reload {target}: {e}",
                hint="Is Ray Serve running? Try: exa stack status",
            )
            return
    count = result.get("count", "?")
    _output.ok(f"Reloaded {count} model(s) into Ray Serve")
    if _output.json_mode:
        _output.print_json(result)


@app.command(epilog=_EXAMPLES_CHECK)
def check() -> None:
    """Smoke test Ray Serve: health check + one prediction per model."""
    cfg = load_config()
    with _output.spinner("Checking Ray Serve health…"):
        try:
            health = _client.get(f"{cfg.ray_serve_url}/health")
        except _client.ClientError as e:
            _output.error(
                f"Ray Serve unreachable: {e}",
                hint="Start it with: exa stack up --service ray-serving",
            )
            return
    _output.ok("Ray Serve is healthy")
    _output.print_record(health)


@app.command("infer-check", epilog=_EXAMPLES_INFER_CHECK)
def infer_check() -> None:
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
    with _output.spinner("Running inference smoke test…"):
        try:
            result = _client.post(f"{cfg.ray_serve_url}/infer-pipeline/infer", body)
        except _client.ClientError as e:
            _output.error(
                f"Inference smoke test failed: {e}",
                hint="Check: exa serve check  then  exa serve reload",
            )
            return
    _output.ok("Inference smoke test passed")
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
    production: int | None = typer.Option(
        None, "--production", help="% traffic to Production alias"
    ),
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
        _output.error(
            f"Traffic weights must sum to 100 — got {total}. Adjust the values and retry."
        )
        raise typer.Exit(1)

    split_str = "  ".join(f"{alias}: {pct}%" for alias, pct in rules.items())
    if not _output.confirm(f"Apply traffic split for [bold]{model}[/bold]? ({split_str})"):
        _output.info("Cancelled.")
        return

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
def benchmark(
    requests: int = typer.Option(200, "--requests", "-n", help="Number of benchmark requests"),
):
    """Benchmark Ray Serve using the dummy client and report latency stats."""
    cmd = [sys.executable, "platform/clients/dummy_client.py", "--benchmark", str(requests)]
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=False)  # noqa: S603
    except FileNotFoundError:
        _output.error("dummy_client.py not found — run exa serve benchmark from the repo root")
    except subprocess.CalledProcessError as e:
        _output.error(f"dummy_client.py exited with code {e.returncode}")


_EXAMPLES_SERVE_MODELS = "Examples:\n\n  exa serve models\n\n  exa serve models --detail"


@app.command("models", epilog=_EXAMPLES_SERVE_MODELS)
def models_cmd(
    detail: bool = typer.Option(False, "--detail", "-d", help="Show full model detail"),
):
    """List models currently hot-loaded in Ray Serve."""
    cfg = load_config()
    try:
        data = _client.get(f"{cfg.ray_serve_url}/models")
    except _client.ClientError as e:
        _output.error(str(e))
        return
    if _output.json_mode:
        _output.print_json(data)
        return
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("models", list(data.values()) if data else [])
    else:
        items = []
    if not items:
        _output.ok("No models loaded in Ray Serve")
        return
    if detail:
        for item in items:
            _output.print_record(item if isinstance(item, dict) else {"name": item})
    else:
        rows = []
        for item in items:
            if isinstance(item, dict):
                rows.append(
                    [
                        item.get("name", "—"),
                        item.get("alias", "—"),
                        item.get("version", "—"),
                        item.get("status", "—"),
                    ]
                )
            else:
                rows.append([str(item), "—", "—", "—"])
        _output.print_table(
            "Ray Serve — Loaded Models", ["Name", "Alias", "Version", "Status"], rows
        )


_EXAMPLES_TRAFFIC_LIST = "Examples:\n\n  exa serve traffic-list\n\n  exa --json serve traffic-list"


@app.command("traffic-list", epilog=_EXAMPLES_TRAFFIC_LIST)
def traffic_list():
    """Show traffic split configuration for all models."""
    import json as _json

    from examlops.platform_db import get_db as _get_db
    from examlops.platform_db import init_db as _init_db

    _init_db()
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT model, rules, updated_at, updated_by FROM traffic_rules ORDER BY model"
        ).fetchall()
    if not rows:
        _output.ok("No traffic rules configured — all models use 100% Production (default)")
        return
    data = [
        {
            "model": r["model"],
            "rules": _json.loads(r["rules"]),
            "updated_at": r["updated_at"],
            "updated_by": r["updated_by"],
        }
        for r in rows
    ]
    if _output.json_mode:
        _output.print_json(data)
        return
    table_rows = []
    for item in data:
        rules_str = "  ".join(f"{alias}:{pct}%" for alias, pct in item["rules"].items())
        table_rows.append(
            [item["model"], rules_str, (item["updated_at"] or "—")[:19], item["updated_by"] or "—"]
        )
    _output.print_table("Traffic Rules", ["Model", "Split", "Updated At", "Updated By"], table_rows)
