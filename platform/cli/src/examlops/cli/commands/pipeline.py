from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._enums import EnvOverlay, StorageBackend
from examlops.platform_db import get_db, get_promotion_rule, init_db, set_promotion_rule, write_audit_event

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

_GENERATOR = "pipelines/pipeline_generator.py"
_DEPLOY = "pipelines/deploy.py"

_EXAMPLES_LIST = "Examples:\n\n  exa pipeline list"
_EXAMPLES_RUN = (
    "Examples:\n\n"
    "  # Fast dev run for every discovered model/dataset (no downloads)\n"
    "  exa pipeline run --dummy\n\n"
    "  # Copy-paste: train one model with dummy data\n"
    "  exa pipeline run --model JPCP --dataset PM100Dataset --dummy\n\n"
    "  # Train one model from MinIO-backed data\n"
    "  exa pipeline run --model JPCP --dataset PM100Dataset --backend minio\n\n"
    "  # Use YAML registry overlays for production settings\n"
    "  exa pipeline run --registry pipelines/model_registry.yaml --env prod"
)
_EXAMPLES_DEPLOY = (
    "Examples:\n\n"
    "  # Register all model deployments with the default nightly schedule\n"
    "  exa pipeline deploy\n\n"
    "  # Register deployments for manual triggering only\n"
    "  exa pipeline deploy --no-schedule\n\n"
    "  # Deploy one model from the YAML registry with staging settings\n"
    "  exa pipeline deploy --model JPCP --registry pipelines/model_registry.yaml --env staging"
)
_EXAMPLES_EXPORT = (
    "Examples:\n\n"
    "  # Snapshot auto-discovered models into pipelines/model_registry.yaml\n"
    "  exa pipeline export-registry"
)
_EXAMPLES_VALIDATE = (
    "Examples:\n\n"
    "  # Validate pipelines/models/*.yaml against Python model shims\n"
    "  exa pipeline validate"
)


def _run_generator(args: list[str]) -> None:
    cmd = [sys.executable, _GENERATOR, *args]
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=False)  # noqa: S603
    except FileNotFoundError:
        _output.error("pipeline_generator.py not found — run exa pipeline commands from the repo root")
    except subprocess.CalledProcessError as e:
        _output.error(f"Pipeline generator exited with code {e.returncode}")


def _run_deploy(args: list[str]) -> None:
    cmd = [sys.executable, _DEPLOY, *args]
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=False)  # noqa: S603
    except FileNotFoundError:
        _output.error("pipelines/deploy.py not found — run exa pipeline deploy from the repo root")
    except subprocess.CalledProcessError as e:
        _output.error(f"deploy.py exited with code {e.returncode}")


def _run_pytest(args: list[str]) -> None:
    cmd = [sys.executable, "-m", "pytest", *args]
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=False)  # noqa: S603
    except FileNotFoundError:
        _output.error("pytest not found — run make install-dev or uv pip install -e '.[dev]'")
    except subprocess.CalledProcessError as e:
        _output.error(f"pytest exited with code {e.returncode}")


@app.command("list", epilog=_EXAMPLES_LIST)
def list_pipelines():
    """List all auto-discovered models and their supported datasets."""
    _run_generator(["--list"])


@app.command(epilog=_EXAMPLES_RUN)
def run(
    model: str | None = typer.Option(None, "--model", "-m", help="Run for a single model only"),
    dataset: str | None = typer.Option(None, "--dataset", "-d", help="Run for a single dataset class only"),
    dummy: bool = typer.Option(False, "--dummy", help="Use dummy data (dev-safe)"),
    backend: StorageBackend | None = typer.Option(None, "--backend", "-b", help="Dataset storage backend"),
    env: EnvOverlay | None = typer.Option(None, "--env", help="YAML registry env overlay"),
    registry: str | None = typer.Option(None, "--registry", help="Path to model_registry.yaml"),
):
    """Run training pipeline(s) locally via Prefect."""
    args: list[str] = []
    if dummy:
        args.append("--dummy")
    if model:
        args += ["--model", model]
    if dataset:
        args += ["--dataset", dataset]
    if backend:
        args += ["--backend", backend]
    if registry:
        args += ["--registry", registry]
    if env:
        args += ["--env", env]
    _run_generator(args)


@app.command(epilog=_EXAMPLES_DEPLOY)
def deploy(
    no_schedule: bool = typer.Option(False, "--no-schedule", help="Deploy without a cron schedule"),
    model: str | None = typer.Option(None, "--model", "-m", help="Deploy for a single model only"),
    registry: str | None = typer.Option(None, "--registry", help="Path to model_registry.yaml"),
    env: EnvOverlay | None = typer.Option(None, "--env", "-e", help="Registry env overlay"),
):
    """Register Prefect deployments for all models (or one model)."""
    args: list[str] = []
    if no_schedule:
        args.append("--no-schedule")
    if model:
        args += ["--model", model]
    if registry:
        args += ["--registry", registry]
    if env:
        args += ["--env", env]
    _run_deploy(args)


@app.command("export-registry", epilog=_EXAMPLES_EXPORT)
def export_registry():
    """Export auto-discovered model state to pipelines/model_registry.yaml."""
    _run_generator(["--export-registry"])


@app.command(epilog=_EXAMPLES_VALIDATE)
def validate():
    """Validate pipelines/models/*.yaml against Python model shims."""
    _run_pytest(["tests/unit/test_registry_integrity.py", "-v", "-k", "yaml"])


_EXAMPLES_VALIDATE_MODEL = (
    "Examples:\n\n"
    "  # Validate JPCP is responding within 2s latency threshold\n"
    "  exa pipeline validate-model JPCP\n\n"
    "  # Validate with custom latency threshold (500ms)\n"
    "  exa pipeline validate-model JPCP --max-latency 0.5\n\n"
    "  # Validate using Staging alias instead of Production\n"
    "  exa pipeline validate-model JPCP --alias Staging"
)

_DUMMY_EMBEDDING = [0.1] * 384


@app.command("validate-model", epilog=_EXAMPLES_VALIDATE_MODEL)
def validate_model(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    alias: str = typer.Option("Staging", "--alias", help="Alias to validate"),
    max_latency: float = typer.Option(2.0, "--max-latency", help="Max acceptable latency in seconds"),
    n_requests: int = typer.Option(3, "--n", help="Number of smoke-test requests"),
):
    """Smoke-test a model alias on Ray Serve: check it responds and meets latency SLA.

    Returns exit code 0 on PASS, 1 on FAIL. Safe to use as a gate before promotion.
    """
    import time

    cfg = load_config()
    body = {
        "embedding": _DUMMY_EMBEDDING,
        "num_nodes": 4,
        "user_id": "validate-gate",
        "model_name": model,
        "alias": alias,
    }

    latencies: list[float] = []
    errors: list[str] = []

    for i in range(n_requests):
        t0 = time.perf_counter()
        try:
            _client.post(f"{cfg.ray_serve_url}/infer-pipeline/infer", body)
            latencies.append(time.perf_counter() - t0)
        except _client.ClientError as e:
            errors.append(str(e))

    if errors:
        _output.error(f"Validation FAIL: {len(errors)}/{n_requests} requests failed — {errors[0]}")
        raise typer.Exit(1)

    avg_latency = sum(latencies) / len(latencies)
    max_observed = max(latencies)
    passed = avg_latency <= max_latency

    row = {
        "model": model,
        "alias": alias,
        "n_requests": n_requests,
        "avg_latency_s": round(avg_latency, 3),
        "max_latency_s": round(max_observed, 3),
        "threshold_s": max_latency,
        "result": "PASS" if passed else "FAIL",
    }

    if _output.json_mode:
        _output.print_json(row)
    else:
        cols = ["Model", "Alias", "Requests", "Avg Latency", "Max Latency", "Threshold", "Result"]
        _output.print_table(
            "Model Validation",
            cols,
            [[model, alias, str(n_requests), f"{avg_latency:.3f}s", f"{max_observed:.3f}s",
              f"{max_latency}s", row["result"]]],
        )

    if not passed:
        _output.error(f"Latency {avg_latency:.3f}s exceeds threshold {max_latency}s")
        raise typer.Exit(1)


_EXAMPLES_PROMOTE = (
    "Examples:\n\n"
    "  # Promote JPCP from Staging to Production if RMSE < 5.0\n"
    "  exa pipeline promote jpcp --if-rmse-lt 5.0\n\n"
    "  # Dry-run: show what would happen without promoting\n"
    "  exa pipeline promote jpcp --if-rmse-lt 5.0 --dry-run\n\n"
    "  # List saved promotion rules\n"
    "  exa pipeline promote --list"
)

_OPS: dict[str, object] = {
    "lt": lambda v, t: v < t,
    "gt": lambda v, t: v > t,
    "lte": lambda v, t: v <= t,
    "gte": lambda v, t: v >= t,
}


@app.command(
    epilog=_EXAMPLES_PROMOTE,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def promote(
    ctx: typer.Context,
    model: str | None = typer.Argument(None, help="Model name (e.g. jpcp); omit with --list"),
    from_alias: str = typer.Option("Staging", "--from", help="Source alias"),
    to_alias: str = typer.Option("Production", "--to", help="Target alias"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show outcome without promoting"),
    save: bool = typer.Option(False, "--save", help="Save rule to DB for future reference"),
    list_rules: bool = typer.Option(False, "--list", help="List saved promotion rules"),
):
    """Promote a model alias when a metric threshold passes (rule-based gate).

    Specify metric threshold with --if-<metric>-<op> <value>, e.g. --if-rmse-lt 5.0
    """
    init_db()

    if list_rules:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM promotion_rules ORDER BY model").fetchall()
        if not rows:
            _output.ok("No saved promotion rules")
            return
        cols = ["Model", "Metric", "Op", "Threshold", "From", "To", "Enabled"]
        table = [
            [r["model"], r["metric"], r["operator"], r["threshold"],
             r["from_alias"], r["to_alias"], "yes" if r["enabled"] else "no"]
            for r in rows
        ]
        _output.print_table("Promotion Rules", cols, table)
        return

    if not model:
        _output.error("Provide a model name or --list")
        return

    # Parse --if-<metric>-<op> VALUE from the extra args captured by context
    metric, operator, threshold = None, None, None
    extra = ctx.args
    for i, arg in enumerate(extra):
        if arg.startswith("--if-") and i + 1 < len(extra):
            parts = arg[5:].rsplit("-", 1)  # e.g. "rmse-lt" -> ["rmse", "lt"]
            if len(parts) == 2 and parts[1] in _OPS:
                metric, operator = parts[0], parts[1]
                try:
                    threshold = float(extra[i + 1])
                except ValueError:
                    _output.error(f"Invalid threshold: {extra[i + 1]}")
                    return
            break

    if metric is None or operator is None or threshold is None:
        _output.error("Specify threshold: --if-<metric>-<op> <value>  e.g. --if-rmse-lt 5.0")
        return

    cfg = load_config()

    try:
        rm_data = _client.get(
            f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/registered-models/get"
            f"?name={urllib.parse.quote(model)}"
        )
    except _client.ClientError as e:
        _output.error(str(e))
        return

    aliases = {a["alias"]: a["version"] for a in rm_data.get("registered_model", {}).get("aliases", [])}
    version = aliases.get(from_alias)
    if not version:
        _output.error(f"No version found under alias '{from_alias}' for {model}")
        return

    try:
        ver_data = _client.get(
            f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/model-versions/get"
            f"?name={urllib.parse.quote(model)}&version={version}"
        )
        run_id = ver_data["model_version"]["run_id"]
        run_data = _client.get(
            f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/runs/get?run_id={run_id}"
        )
    except _client.ClientError as e:
        _output.error(str(e))
        return

    metrics = run_data.get("run", {}).get("data", {}).get("metrics", {})
    metric_val = metrics.get(metric)
    if metric_val is None:
        _output.error(f"Metric '{metric}' not found in run {run_id}. Available: {list(metrics.keys())}")
        return

    op_fn = _OPS[operator]
    passes = op_fn(metric_val, threshold)  # type: ignore[operator]
    op_sym = "<" if operator in ("lt", "lte") else ">"
    status_str = f"{metric}={metric_val:.4f}  {op_sym}{threshold}"

    if dry_run:
        verdict = "WOULD promote" if passes else "Would NOT promote"
        _output.ok(f"[DRY RUN] {model} v{version}: {status_str}  → {verdict} to {to_alias}")
        return

    if not passes:
        _output.ok(f"Not promoted: {model} v{version}: {status_str}  (threshold not met)")
        return

    try:
        _client.post(
            f"{cfg.mlflow_url}/ajax-api/2.0/mlflow/registered-models/alias",
            {"name": model, "alias": to_alias, "version": version},
        )
    except _client.ClientError as e:
        _output.error(f"Promotion failed: {e}")
        return

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event("cli", actor, "alias_promoted", model,
                      {"from": from_alias, "to": to_alias, "version": version, metric: metric_val})

    if save:
        set_promotion_rule(model, metric, operator, threshold, from_alias, to_alias)

    _output.ok(f"Promoted {model} v{version} → {to_alias}  ({status_str})")
