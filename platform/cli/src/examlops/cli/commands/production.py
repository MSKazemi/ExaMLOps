"""exa production — production deployment and verification workflows."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._enums import EnvOverlay

app = typer.Typer(no_args_is_help=True, rich_markup_mode="rich")

_EXAMPLES_VERIFY = (
    "Examples:\n\n"
    "  exa production verify\n\n"
    "  exa --json production verify"
)
_EXAMPLES_DEPLOY = (
    "Examples:\n\n"
    "  exa production deploy\n\n"
    "  exa production deploy --models stale --env prod\n\n"
    "  exa production deploy --execute --models JPCP,MACK --dataset FDataDataset"
)

_DEPLOY = "pipelines/deploy.py"
_HISTORY_PATH = Path("platform/state/deployments/history.jsonl")


@dataclass
class CheckResult:
    name: str
    ok: bool
    status: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "details": self.details,
        }


def _safe_get(url: str, *, token: str = "") -> tuple[bool, Any, str]:
    try:
        return True, _client.get(url, token=token), "reachable"
    except _client.ClientError as exc:
        return False, None, str(exc)


def _count_stale_models(modelzoo: dict[str, Any]) -> int:
    return sum(1 for model in modelzoo.get("models", []) if model.get("status") == "stale")


def _modelzoo_models(cfg) -> list[dict[str, Any]]:
    reachable, data, message = _safe_get(f"{cfg.control_plane_url}/modelzoo/status")
    if not reachable or not isinstance(data, dict):
        _output.error(f"Unable to read ModelZoo status: {message}")
    models = data.get("models", [])
    if not isinstance(models, list):
        _output.error("Unexpected ModelZoo status response: models is not a list")
    return models


def _select_models(models: list[dict[str, Any]], selector: str) -> list[str]:
    if selector == "stale":
        return [str(m.get("model_id")) for m in models if m.get("status") == "stale"]
    if selector == "all":
        return [str(m.get("model_id")) for m in models]
    return [m.strip() for m in selector.split(",") if m.strip()]


def _deploy_pipelines(registry: str, env: EnvOverlay, no_schedule: bool) -> dict[str, Any]:
    cmd = [sys.executable, _DEPLOY, "--registry", registry, "--env", env.value]
    if no_schedule:
        cmd.append("--no-schedule")
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=False)  # noqa: S603
    except FileNotFoundError:
        _output.error("pipelines/deploy.py not found — run exa production deploy from the repo root")
    except subprocess.CalledProcessError as exc:
        _output.error(f"Production pipeline deployment failed with code {exc.returncode}")
    return {"status": "success", "command": cmd}


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_deploy_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"deploy-{stamp}-{uuid.uuid4().hex[:8]}"


def _read_history() -> list[dict[str, Any]]:
    if not _HISTORY_PATH.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in _HISTORY_PATH.read_text().splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _append_history(record: dict[str, Any]) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _previous_successful_deploy_id() -> str | None:
    for record in reversed(_read_history()):
        if record.get("status") == "success":
            return str(record.get("deploy_id"))
    return None


def _find_deploy_record(deploy_id: str) -> dict[str, Any] | None:
    return next((record for record in _read_history() if record.get("deploy_id") == deploy_id), None)


def _production_model_versions(record: dict[str, Any]) -> list[dict[str, str]]:
    versions: list[dict[str, str]] = []
    for model in record.get("loaded_models", []):
        if model.get("alias") != "Production":
            continue
        name = model.get("model_name") or model.get("name") or model.get("model")
        version = model.get("model_version") or model.get("version")
        if name and version:
            versions.append({"model": str(name), "version": str(version)})
    return versions


def _set_production_alias(cfg, model_name: str, version: str) -> dict[str, str]:
    try:
        import mlflow
    except ImportError:
        _output.error("mlflow is required to restore Production aliases")
    mlflow.set_tracking_uri(cfg.mlflow_url)
    client = mlflow.MlflowClient()
    client.set_registered_model_alias(model_name, "Production", version)
    return {"model": model_name, "alias": "Production", "version": version}


def _loaded_models_snapshot(cfg) -> list[dict[str, Any]]:
    reachable, models, _ = _safe_get(f"{cfg.ray_serve_url}/models")
    if reachable and isinstance(models, list):
        return models
    return []


def _rollback(deploy_id: str, *, execute: bool) -> None:
    target = _find_deploy_record(deploy_id)
    if target is None:
        _output.error(f"Deploy record not found: {deploy_id}")
    previous_id = target.get("rollback", {}).get("previous_successful_deploy_id")
    if not previous_id:
        _output.error(f"Deploy record has no previous successful deploy: {deploy_id}")
    previous = _find_deploy_record(str(previous_id))
    if previous is None:
        _output.error(f"Previous successful deploy record not found: {previous_id}")
    model_versions = _production_model_versions(previous)
    if not model_versions:
        _output.error(f"Previous deploy has no Production model version snapshot: {previous_id}")

    if not execute:
        if _output.json_mode:
            _output.print_json(
                {
                    "mode": "dry_run",
                    "rollback_of_deploy_id": deploy_id,
                    "restored_deploy_id": previous_id,
                    "models": model_versions,
                    "steps": ["restore Production aliases", "reload Ray Serve", "verify production"],
                }
            )
            return
        _output.console.print("[yellow]ROLLBACK DRY RUN[/yellow] — no production state will be changed.")
        _output.console.print(f"rollback_of={deploy_id} restore_to={previous_id}")
        for model in model_versions:
            _output.console.print(f"restore {model['model']} Production alias to v{model['version']}")
        return

    cfg = load_config()
    record: dict[str, Any] = {
        "deploy_id": _new_deploy_id(),
        "operation": "rollback",
        "started_at": _now_iso(),
        "rollback_of_deploy_id": deploy_id,
        "restored_deploy_id": str(previous_id),
        "models": [m["model"] for m in model_versions],
        "status": "running",
    }

    def fail_record(step: str, message: str) -> None:
        record.update({"ended_at": _now_iso(), "status": "failed", "failed_step": step, "error": message})
        _append_history(record)

    _output.console.print("[bold red]ROLLBACK EXECUTE[/bold red] — restoring previous Production aliases.")
    alias_results: list[dict[str, str]] = []
    for model in model_versions:
        try:
            alias_results.append(_set_production_alias(cfg, model["model"], model["version"]))
        except Exception as exc:  # noqa: BLE001
            fail_record(f"alias:{model['model']}", str(exc))
            _output.error(f"Rollback alias restore failed for {model['model']}: {exc}")
    record["alias_results"] = alias_results

    try:
        reload_result = _client.post(f"{cfg.ray_serve_url}/reload", {})
    except _client.ClientError as exc:
        fail_record("reload", str(exc))
        _output.error(f"Ray Serve reload failed: {exc}")
        return
    record["reload_result"] = reload_result

    checks = _run_verification_checks(cfg)
    overall_pass = all(c.ok for c in checks)
    verification = {c.name.lower().replace(" ", "_"): c.as_dict() for c in checks}
    record.update(
        {
            "ended_at": _now_iso(),
            "status": "success" if overall_pass else "partial",
            "verification_status": "pass" if overall_pass else "fail",
            "verification": verification,
            "loaded_models": _loaded_models_snapshot(cfg),
        }
    )
    _append_history(record)

    if _output.json_mode:
        _output.print_json(record)
        raise typer.Exit(0 if overall_pass else 1)
    _output.print_table(
        "Rollback Aliases",
        ["Model", "Alias", "Version"],
        [[r["model"], r["alias"], r["version"]] for r in alias_results],
    )
    _output.ok(f"Reloaded {reload_result.get('count', '?')} model(s)")
    if overall_pass:
        _output.ok(f"Rollback completed ({record['deploy_id']})")
    else:
        _output.error("Rollback completed but verification failed", exit_code=1)


def _record_matches_filters(
    record: dict[str, Any],
    *,
    status: str | None,
    model: str | None,
    operation: str | None,
) -> bool:
    if status and record.get("status") != status:
        return False
    if operation and record.get("operation", "deploy") != operation:
        return False
    if model and model not in {str(m) for m in record.get("models", [])}:
        return False
    return True


def _history(limit: int, *, status: str | None = None, model: str | None = None, operation: str | None = None) -> None:
    records = [
        record
        for record in reversed(_read_history())
        if _record_matches_filters(record, status=status, model=model, operation=operation)
    ][:limit]
    if _output.json_mode:
        _output.print_json(records)
        return
    if not records:
        _output.console.print("No production deploy history found.")
        return
    _output.console.print("[bold]Production Deploy History[/bold]")
    for r in records:
        _output.console.print(
            f"{r.get('deploy_id')}  status={r.get('status')}  env={r.get('env')}  "
            f"models={','.join(r.get('models', []))}  started={r.get('started_at')}  "
            f"verification={r.get('verification_status', '—')}"
        )


def _status(deploy_id: str) -> None:
    record = _find_deploy_record(deploy_id)
    if record is None:
        _output.error(f"Deploy record not found: {deploy_id}")
    if _output.json_mode:
        _output.print_json(record)
        return
    _output.print_record(
        {
            "deploy_id": record.get("deploy_id"),
            "status": record.get("status"),
            "env": record.get("env"),
            "models": ", ".join(record.get("models", [])),
            "dataset": record.get("dataset"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at", "—"),
            "verification_status": record.get("verification_status", "—"),
            "rollback": record.get("rollback", {}),
        }
    )


def _check_control_plane(cfg) -> CheckResult:
    reachable, data, message = _safe_get(f"{cfg.control_plane_url}/health")
    ok = reachable and isinstance(data, dict) and data.get("status") == "ok"
    return CheckResult(
        "Control Plane",
        ok,
        "ok" if ok else "unreachable",
        {
            "pending_approvals": (data or {}).get("pending_approvals", 0) if isinstance(data, dict) else 0,
            "message": message,
        },
    )


def _check_ray_serve(cfg) -> CheckResult:
    health_ok, health, health_message = _safe_get(f"{cfg.ray_serve_url}/health")
    models_ok, models, models_message = _safe_get(f"{cfg.ray_serve_url}/models")
    model_count = len(models) if isinstance(models, list) else 0
    ok = health_ok and models_ok and isinstance(health, dict) and health.get("status") == "ok" and model_count > 0
    return CheckResult(
        "Ray Serve",
        ok,
        f"{model_count} model(s)" if ok else "unreachable",
        {
            "models": model_count,
            "health_message": health_message,
            "models_message": models_message,
        },
    )


def _check_modelzoo(cfg) -> CheckResult:
    reachable, data, message = _safe_get(f"{cfg.control_plane_url}/modelzoo/status")
    stale = _count_stale_models(data) if isinstance(data, dict) else 0
    total = len(data.get("models", [])) if isinstance(data, dict) else 0
    ok = reachable and isinstance(data, dict)
    status = f"STALE: {stale}" if stale else f"current ({total})"
    return CheckResult(
        "ModelZoo",
        ok,
        status if ok else "unreachable",
        {"models": total, "stale_models": stale, "message": message},
    )


def _check_dashboard(cfg) -> CheckResult:
    reachable, data, message = _safe_get(f"{cfg.dashboard_url}/api/health")
    ok = reachable and isinstance(data, dict) and data.get("status") == "ok"
    return CheckResult(
        "Dashboard",
        ok,
        "ok" if ok else "unreachable",
        {"message": message},
    )


def _check_seanerbus() -> CheckResult:
    health_ok, health, health_message = _safe_get("http://localhost:18003/health")
    stats_ok, stats, stats_message = _safe_get("http://localhost:18003/stats")
    inferences = stats.get("inferences_total", 0) if isinstance(stats, dict) else 0
    ok = health_ok and stats_ok and isinstance(health, dict) and health.get("status") == "ok"
    return CheckResult(
        "SeanerBUS",
        ok,
        f"{inferences} inference(s)" if ok else "unreachable",
        {
            "inferences_total": inferences,
            "health_message": health_message,
            "stats_message": stats_message,
        },
    )


def _run_verification_checks(cfg) -> list[CheckResult]:
    return [
        _check_control_plane(cfg),
        _check_ray_serve(cfg),
        _check_modelzoo(cfg),
        _check_dashboard(cfg),
        _check_seanerbus(),
    ]


@app.command("verify", epilog=_EXAMPLES_VERIFY)
def verify():
    """Verify production service health without changing state."""
    cfg = load_config()
    checks = _run_verification_checks(cfg)
    stale_models = next((c.details.get("stale_models", 0) for c in checks if c.name == "ModelZoo"), 0)
    overall_pass = all(c.ok for c in checks)

    result = {
        "overall_status": "pass" if overall_pass else "fail",
        "stale_models": stale_models,
        "checks": {c.name.lower().replace(" ", "_"): c.as_dict() for c in checks},
    }
    if _output.json_mode:
        _output.print_json(result)
        raise typer.Exit(0 if overall_pass else 1)

    rows = [[c.name, "✓" if c.ok else "✗", c.status] for c in checks]
    _output.print_table("Production Verification", ["Check", "OK", "Status"], rows)
    if overall_pass:
        _output.ok(f"PASS — production services reachable; stale models: {stale_models}")
    else:
        _output.error("FAIL — one or more production checks failed", exit_code=1)


@app.command("deploy", epilog=_EXAMPLES_DEPLOY)
def deploy(
    action: str | None = typer.Argument(
        None,
        help="Optional history action: 'history' or 'status'. Omit to plan/run a deploy.",
    ),
    deploy_id: str | None = typer.Argument(None, help="Deploy ID for 'status'."),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually deploy/retrain/reload. Default is a side-effect-free dry run.",
    ),
    models: str = typer.Option(
        "stale",
        "--models",
        help="Model selector: 'stale', 'all', or comma-separated IDs such as JPCP,MACK.",
    ),
    dataset: str = typer.Option("FDataDataset", "--dataset", help="Dataset for retraining selected models"),
    env: EnvOverlay = typer.Option(EnvOverlay.prod, "--env", "-e", help="Registry env overlay"),
    registry: str = typer.Option(
        "pipelines/model_registry.yaml",
        "--registry",
        help="Path to model_registry.yaml",
    ),
    no_schedule: bool = typer.Option(False, "--no-schedule", help="Deploy Prefect flows without schedules"),
    limit: int = typer.Option(20, "--limit", help="Number of deploy history records to show."),
    history_status: str | None = typer.Option(None, "--status", help="Filter deploy history by status."),
    history_model: str | None = typer.Option(None, "--model", help="Filter deploy history by model ID."),
    history_operation: str | None = typer.Option(None, "--operation", help="Filter deploy history by operation."),
):
    """Plan/execute production deploys, or inspect deploy history/status."""
    if action == "history":
        _history(limit, status=history_status, model=history_model, operation=history_operation)
        return
    if action == "status":
        if not deploy_id:
            _output.error("Usage: exa production deploy status <deploy-id>")
        assert deploy_id is not None
        _status(deploy_id)
        return
    if action == "rollback":
        if not deploy_id:
            _output.error("Usage: exa production deploy rollback <deploy-id>")
        assert deploy_id is not None
        _rollback(deploy_id, execute=execute)
        return
    if action is not None:
        _output.error(f"Unknown deploy action: {action}")

    cfg = load_config()
    model_status = _modelzoo_models(cfg)
    selected = _select_models(model_status, models)
    mode = "EXECUTE" if execute else "DRY RUN"

    plan = {
        "mode": mode.lower().replace(" ", "_"),
        "models": selected,
        "dataset": dataset,
        "env": env.value,
        "registry": registry,
        "steps": [
            "pipeline deploy",
            "retrain selected models",
            "reload Ray Serve",
            "verify production",
        ],
    }

    if not execute:
        if _output.json_mode:
            _output.print_json(plan)
            return
        _output.console.print(f"[yellow]{mode}[/yellow] — no production state will be changed.")
        _output.print_table(
            "Production Deploy Plan",
            ["Step", "Action"],
            [
                ["1", f"pipeline deploy --registry {registry} --env {env.value}"],
                ["2", f"retrain {', '.join(selected) or '(none)'} using {dataset}"],
                ["3", "reload Ray Serve"],
                ["4", "run production verification"],
            ],
        )
        return

    deploy_record: dict[str, Any] = {
        "deploy_id": _new_deploy_id(),
        "started_at": _now_iso(),
        "env": env.value,
        "registry": registry,
        "models": selected,
        "dataset": dataset,
        "status": "running",
        "verification_status": None,
        "rollback": {"previous_successful_deploy_id": _previous_successful_deploy_id()},
    }

    def fail_record(step: str, message: str) -> None:
        deploy_record.update(
            {
                "ended_at": _now_iso(),
                "status": "failed",
                "failed_step": step,
                "error": message,
            }
        )
        _append_history(deploy_record)

    _output.console.print(f"[bold red]{mode}[/bold red] — applying production workflow.")
    try:
        pipeline_deploy = _deploy_pipelines(registry, env, no_schedule)
    except typer.Exit:
        fail_record("pipeline_deploy", "Production pipeline deployment failed")
        raise
    deploy_record["pipeline_deploy"] = pipeline_deploy

    retrain_results = []
    for model in selected:
        body = {
            "model_name": model,
            "dataset_name": dataset,
            "is_dummy": False,
            "backend_name": None,
        }
        try:
            result = _client.post(f"{cfg.control_plane_url}/retrain", body, token=cfg.control_plane_token)
        except _client.ClientError as exc:
            fail_record(f"retrain:{model}", str(exc))
            _output.error(f"Retrain failed for {model}: {exc}")
            return
        retrain_results.append({"model": model, "flow_run_id": result.get("flow_run_id")})
    deploy_record["retrain_results"] = retrain_results

    try:
        reload_result = _client.post(f"{cfg.ray_serve_url}/reload", {})
    except _client.ClientError as exc:
        fail_record("reload", str(exc))
        _output.error(f"Ray Serve reload failed: {exc}")
        return
    deploy_record["reload_result"] = reload_result

    checks = _run_verification_checks(cfg)
    overall_pass = all(c.ok for c in checks)
    verification = {c.name.lower().replace(" ", "_"): c.as_dict() for c in checks}
    deploy_record.update(
        {
            "ended_at": _now_iso(),
            "status": "success" if overall_pass else "partial",
            "verification_status": "pass" if overall_pass else "fail",
            "verification": verification,
            "loaded_models": _loaded_models_snapshot(cfg),
        }
    )
    _append_history(deploy_record)

    result = {
        **plan,
        "deploy_id": deploy_record["deploy_id"],
        "retrain_results": retrain_results,
        "reload_result": reload_result,
        "verification": verification,
        "overall_status": "pass" if overall_pass else "fail",
        "history_record": str(_HISTORY_PATH),
    }
    if _output.json_mode:
        _output.print_json(result)
        raise typer.Exit(0 if overall_pass else 1)

    _output.print_table(
        "Retrain Runs",
        ["Model", "Flow Run"],
        [[r["model"], r["flow_run_id"] or "—"] for r in retrain_results],
    )
    _output.ok(f"Reloaded {reload_result.get('count', '?')} model(s)")
    _output.print_table(
        "Production Verification",
        ["Check", "OK", "Status"],
        [[c.name, "✓" if c.ok else "✗", c.status] for c in checks],
    )
    if overall_pass:
        _output.ok(f"Production deploy workflow completed ({deploy_record['deploy_id']})")
    else:
        _output.error("Production deploy completed but verification failed", exit_code=1)
