from __future__ import annotations

import os
import subprocess
import sys

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._help import make_ordered_group
from examlops.cli._provenance import audit_details, reason_option
from examlops.cli.commands import explain_cmd
from examlops.data import init_db
from examlops.data.audit import write_audit_event
from examlops.data.serving import get_traffic_rules, set_traffic_rules

# Help panels for `exa serve` (title order = on-screen order). Applied in main.py after the
# sub-typers (shadow/challenger/autoscale/routing/batch/ab/adapter) are attached there.
_PANELS: list[tuple[str, list[str]]] = [
    ("Health & Deploy", ["reload", "check", "infer-check", "benchmark", "manifest", "backend"]),
    ("LLM & VLM Serving", ["llm"]),
    ("Traffic & Routing", ["traffic", "traffic-list", "routing", "shadow", "ab"]),
    ("Scaling & Batch", ["autoscale", "batch"]),
    ("Models & Adapters", ["models", "adapter", "challenger", "explain"]),
]

app = typer.Typer(
    cls=make_ordered_group(_PANELS),
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
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the split that would be applied without changing routing"
    ),
    reason: str | None = reason_option(),
):
    """Show or set traffic split across model aliases (must sum to 100)."""
    init_db()

    if production is None and canary is None and staging is None:
        # Named apart from the `rules` built below: one is what is stored (and may be absent),
        # the other is what this invocation is about to set. Sharing the name made every use of
        # the second one an optional-typed value, which is how ten type errors came from one
        # variable that is never None where it is used.
        current = get_traffic_rules(model)
        if current is None:
            _output.ok(f"No traffic rules for {model} — 100% Production (default)")
            return
        if _output.json_mode:
            _output.print_json(current)
        else:
            rows = [[alias, f"{pct}%"] for alias, pct in current.items()]
            _output.print_table(f"Traffic split — {model}", ["Alias", "Weight"], rows)
        return

    rules: dict[str, int] = {}
    if production is not None:
        rules["Production"] = production
    if canary is not None:
        rules["Canary"] = canary
    if staging is not None:
        rules["Staging"] = staging

    negative = {alias: pct for alias, pct in rules.items() if pct < 0}
    if negative:
        bad = "  ".join(f"{alias}: {pct}" for alias, pct in negative.items())
        _output.error(f"Traffic weights must be non-negative — got {bad}.")
        raise typer.Exit(1)

    total = sum(rules.values())
    if total != 100:
        _output.error(
            f"Traffic weights must sum to 100 — got {total}. Adjust the values and retry."
        )
        raise typer.Exit(1)

    split_str = "  ".join(f"{alias}: {pct}%" for alias, pct in rules.items())

    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "model": model, "would_apply": rules})
        else:
            _output.info(f"Dry run — traffic split for {model} would be set to: {split_str}")
        return

    if not _output.confirm(f"Apply traffic split for [bold]{model}[/bold]? ({split_str})"):
        _output.info("Cancelled.")
        return

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    set_traffic_rules(model, rules, actor)
    write_audit_event("cli", actor, "traffic_changed", model, audit_details(rules, reason))

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
def traffic_list(
    watch: bool = typer.Option(
        False, "--watch", "-w", help="Live auto-refreshing view (Ctrl-C to exit)"
    ),
    interval: int = typer.Option(5, "--interval", help="Refresh interval in seconds for --watch"),
):
    """Show traffic split configuration for all models."""
    if watch and not _output.json_mode:
        _output.watch_loop(_render_traffic_list, interval)
        return
    _render_traffic_list()


def _render_traffic_list() -> None:
    import json as _json

    from examlops.data import get_db as _get_db
    from examlops.data import init_db as _init_db

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


# ── E1 — Kubernetes-native serving (ADR 0015) ─────────────────────────────────

_EXAMPLES_MANIFEST = (
    "Examples:\n\n"
    "  exa serve manifest JPCP\n\n"
    "  exa serve manifest JPCP --canary 10 --canary-alias Canary --out ./k8s/jpcp.yaml\n\n"
    "  exa serve manifest JPCP --version 17 --artifact-uri s3://mlflow-artifacts/1/models/m-1/artifacts\n\n"
    "  exa --json serve manifest JPCP"
)


@app.command("manifest", epilog=_EXAMPLES_MANIFEST)
def manifest(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    alias: str = typer.Option("Production", "--alias", help="MLflow alias to serve"),
    version: str | None = typer.Option(
        None, "--version", help="Serve this registered version instead of resolving --alias"
    ),
    artifact_uri: str | None = typer.Option(
        None,
        "--artifact-uri",
        help="Render offline from this storage URI (s3://, oci://, hf://…); requires --version",
    ),
    canary: int | None = typer.Option(
        None, "--canary", help="Canary traffic percent (0..100) for the --canary-alias version"
    ),
    canary_alias: str = typer.Option("Canary", "--canary-alias", help="MLflow alias of the canary"),
    out: str | None = typer.Option(None, "--out", help="Write manifest YAML to this file"),
    registry_dir: str | None = typer.Option(
        None, "--registry-dir", help="Dir of per-model YAML (default: RAY_MODELS_DIR)"
    ),
) -> None:
    """Render a KServe manifest for a resolved model version, checked against the pinned schema.

    The alias is resolved to a concrete version and its artifact URI before rendering, so the
    manifest names exactly what would run. Nothing is applied to a cluster.
    """
    from pathlib import Path

    import yaml

    from examlops.serving.substrates.resolve import RenderError, resolve_ref
    from examlops.serving_backends import registry_to_kserve, validate_manifest
    from examlops.usecase import models_dir

    reg = registry_dir or os.getenv("RAY_MODELS_DIR") or str(models_dir())
    yaml_path = Path(reg) / f"{model.lower()}.yaml"
    if not yaml_path.is_file():
        _output.error(f"Model YAML not found: {yaml_path}")
    model_yaml = yaml.safe_load(yaml_path.read_text())
    project = str(model_yaml.get("project") or "default")
    offline_hint = "pass --version and --artifact-uri to render without the registry"
    if canary is not None and artifact_uri is not None:
        _output.error(
            "--canary resolves both versions from the registry; it cannot be combined with "
            "--artifact-uri"
        )
    try:
        stable = resolve_ref(
            model, alias=alias, version=version, artifact_uri=artifact_uri, project=project
        )
        canary_ref = None
        if canary is not None:
            canary_ref = resolve_ref(model, alias=canary_alias, project=project)
        m = registry_to_kserve(model_yaml, stable, canary=canary_ref, canary_pct=canary)
    except RenderError as exc:
        _output.error(f"Cannot render a manifest for {model}: {exc}")
    except Exception as exc:  # noqa: BLE001 - registry unreachable, unknown alias, …
        _output.error(f"Could not resolve {model} in the MLflow registry: {exc}", hint=offline_hint)
    errors = validate_manifest(m)
    if errors:
        _output.error(f"Generated manifest failed validation: {errors}")
    rendered = yaml.safe_dump(m, sort_keys=False)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(rendered)
        _output.ok(f"KServe manifest written to {out} ({m['kind']})")
        return
    if _output.json_mode:
        _output.print_json(m)
        return
    typer.echo(rendered)
    _output.ok(f"Generated {m['kind']} for {model}@{alias}")


@app.command("backend")
def backend() -> None:
    """Show the active serving backend (ray-compose default | kserve-k8s)."""
    from examlops.serving_backends import select_backend

    b = select_backend()
    if _output.json_mode:
        _output.print_json({"backend": b.name})
        return
    _output.ok(f"Active serving backend: {b.name}  (EXAMLOPS_SERVING_BACKEND)")
