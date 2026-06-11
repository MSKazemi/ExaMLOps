from __future__ import annotations

import math
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from langchain_core.tools import tool

from exa_agent import config
from exa_agent.confirm import confirmed_write
from exa_agent.tools import _http

# Allow importing platform_db from the examlops CLI package
_CLI_SRC = Path(__file__).resolve().parents[4] / "cli" / "src"
if str(_CLI_SRC) not in sys.path:
    sys.path.insert(0, str(_CLI_SRC))

try:
    from examlops.platform_db import (
        get_db,
        get_drift_baseline,
        get_input_baseline,
        init_db,
        list_drift_auto_retrain,
        record_drift_trigger,
        set_traffic_rules,
        write_audit_event,
    )
    init_db()
    _DB_OK = True
except Exception:
    _DB_OK = False


def _db_unavailable() -> str:
    return "platform.db not available — set PLATFORM_DB env var and ensure the CLI is installed"


@tool
def compare_model_versions(model_name: str, v1: str, v2: str) -> str:
    """Compare metrics and parameters between two model versions.

    Args:
        model_name: Registered model name (e.g. 'jpcp').
        v1: First version number.
        v2: Second version number.
    """
    def _get_run(version: str) -> tuple[dict, dict]:
        ver_data, err = _http.request_json(
            "mlflow", "GET",
            f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/model-versions/get"
            f"?name={urllib.parse.quote(model_name)}&version={version}",
        )
        if err:
            return {}, {}
        run_id = ver_data["model_version"]["run_id"]
        run_data, err = _http.request_json(
            "mlflow", "GET",
            f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/runs/get?run_id={run_id}",
        )
        if err:
            return {}, {}
        d = run_data["run"]["data"]
        return d.get("metrics", {}), d.get("params", {})

    m1, p1 = _get_run(v1)
    m2, p2 = _get_run(v2)
    lines = [f"Comparison: {model_name} v{v1} → v{v2}", "Metrics:"]
    for k in sorted(set(m1) | set(m2)):
        val1, val2 = m1.get(k), m2.get(k)
        if val1 is not None and val2 is not None:
            delta = val2 - val1
            lines.append(f"  {k}: {val1:.4f} → {val2:.4f}  (Δ {delta:+.4f})")
        else:
            lines.append(f"  {k}: {val1} → {val2}")
    lines.append("Params:")
    for k in sorted(set(p1) | set(p2)):
        val1, val2 = p1.get(k, "—"), p2.get(k, "—")
        changed = val1 != val2
        lines.append(f"  {k}: {val1} → {val2}{'  [changed]' if changed else ''}")
    return "\n".join(lines)


@tool
def get_model_lineage(model_name: str, version: str = "") -> str:
    """Show the pipeline → dataset → model version lineage chain.

    Args:
        model_name: Registered model name (e.g. 'jpcp').
        version: Version number (default: Production alias).
    """
    if not version:
        rm_data, err = _http.request_json(
            "mlflow", "GET",
            f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/registered-models/get"
            f"?name={urllib.parse.quote(model_name)}",
        )
        if err:
            return err
        aliases = {a["alias"]: a["version"] for a in rm_data.get("registered_model", {}).get("aliases", [])}
        version = aliases.get("Production") or next(iter(aliases.values()), "")
        if not version:
            return f"No versions found for {model_name}"

    ver_data, err = _http.request_json(
        "mlflow", "GET",
        f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/model-versions/get"
        f"?name={urllib.parse.quote(model_name)}&version={version}",
    )
    if err:
        return err
    run_id = ver_data["model_version"]["run_id"]

    run_data, err = _http.request_json(
        "mlflow", "GET",
        f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/runs/get?run_id={run_id}",
    )
    if err:
        return err
    d = run_data["run"]["data"]
    tags = {t["key"]: t["value"] for t in d.get("tags", [])}

    return (
        f"Lineage — {model_name} v{version}\n"
        f"  Pipeline run:    {tags.get('prefect_flow_run_id', 'unknown')}\n"
        f"  Dataset version: {tags.get('dataset_version', 'unknown')}  "
        f"(rows: {tags.get('training_rows', 'unknown')})\n"
        f"  MLflow run:      {run_id}\n"
        f"  Model version:   {model_name} v{version}"
    )


@tool
def get_drift_status(model_name: str = "") -> str:
    """Show prediction drift status for all models or one model.

    Args:
        model_name: Filter to a specific model (empty = all models).
    """
    if not _DB_OK:
        return _db_unavailable()
    with get_db() as conn:
        if model_name:
            rows = conn.execute(
                "SELECT model, prediction FROM drift_snapshots WHERE model=? ORDER BY ts DESC LIMIT 100",
                (model_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT model, prediction FROM drift_snapshots ORDER BY ts DESC LIMIT 1000"
            ).fetchall()
    if not rows:
        return "No drift snapshots recorded yet. Run exa drift baseline <MODEL> after collecting predictions."

    grouped: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        grouped[r["model"]].append(r["prediction"])

    lines = []
    for model, preds in grouped.items():
        n = len(preds)
        mean = sum(preds) / n
        std = math.sqrt(sum((p - mean) ** 2 for p in preds) / n)
        baseline = get_drift_baseline(model)
        if baseline and baseline["std"] > 0:
            z = abs(mean - baseline["mean"]) / baseline["std"]
            status = "CRITICAL" if z >= 3.0 else "WARNING" if z >= 2.0 else "OK"
        else:
            z, status = 0.0, "OK (no baseline)"
        lines.append(f"- {model}: mean={mean:.3f} std={std:.3f} z={z:.2f} → {status}")
    return "\n".join(lines)


@tool
def query_audit_log(last_days: int = 7, model: str = "", action: str = "") -> str:
    """Query the platform audit log.

    Args:
        last_days: How many days to look back (default 7).
        model: Filter by target model name.
        action: Filter by action type (e.g. 'model_approved', 'retrain_triggered').
    """
    if not _DB_OK:
        return _db_unavailable()
    since = (datetime.utcnow() - timedelta(days=last_days)).strftime("%Y-%m-%d %H:%M:%S")
    query = "SELECT ts, source, actor, action, target, details FROM audit_events WHERE ts >= ?"
    params: list = [since]
    if model:
        query += " AND target=?"
        params.append(model)
    if action:
        query += " AND action=?"
        params.append(action)
    query += " ORDER BY ts DESC LIMIT 50"
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        return f"No audit events in the last {last_days} days matching those filters."
    lines = []
    for r in rows:
        details = f"  details={r['details'][:60]}" if r["details"] else ""
        lines.append(
            f"[{r['ts']}] {r['source']}/{r['actor']} → {r['action']} on {r['target']}{details}"
        )
    return "\n".join(lines)


def _set_traffic_summary(model_name: str, production: int = 100, canary: int = 0, staging: int = 0) -> str:
    return f"Set traffic split for {model_name}: Production={production}% Canary={canary}% Staging={staging}%"


@tool
@confirmed_write(_set_traffic_summary)
def set_traffic_split(model_name: str, production: int = 100, canary: int = 0, staging: int = 0) -> str:
    """Set traffic split across model aliases (must sum to 100).

    Args:
        model_name: Model name (e.g. 'JPCP').
        production: Percentage to Production alias (default 100).
        canary: Percentage to Canary alias (default 0).
        staging: Percentage to Staging alias (default 0).
    """
    if not _DB_OK:
        return _db_unavailable()
    total = production + canary + staging
    if total != 100:
        return f"Error: weights must sum to 100, got {total}"
    rules: dict[str, int] = {}
    if production:
        rules["Production"] = production
    if canary:
        rules["Canary"] = canary
    if staging:
        rules["Staging"] = staging
    set_traffic_rules(model_name, rules, "agent")
    write_audit_event("agent", "agent", "traffic_changed", model_name, rules)
    data, err = _http.request_json(
        "ray_serve", "POST",
        f"{config.RAY_SERVE_URL}/traffic-rules/{model_name}",
        json=rules,
    )
    if err:
        return f"Rules saved to DB but could not apply to Ray Serve: {err}"
    return f"Traffic split set for {model_name}: {rules}"


def _promote_summary(
    model_name: str,
    metric: str,
    operator: str,
    threshold: float,
    from_alias: str = "Staging",
    to_alias: str = "Production",
    dry_run: bool = False,
) -> str:
    op_sym = {"lt": "<", "gt": ">", "lte": "<=", "gte": ">="}.get(operator, operator)
    action = "[DRY RUN] " if dry_run else ""
    return f"{action}Promote {model_name} from {from_alias} to {to_alias} if {metric} {op_sym} {threshold}"


@tool
@confirmed_write(_promote_summary)
def promote_model(
    model_name: str,
    metric: str,
    operator: str,
    threshold: float,
    from_alias: str = "Staging",
    to_alias: str = "Production",
    dry_run: bool = False,
) -> str:
    """Promote a model alias if a metric threshold passes.

    Args:
        model_name: Model name (e.g. 'jpcp').
        metric: MLflow metric key (e.g. 'rmse', 'mae').
        operator: Comparison operator — 'lt', 'gt', 'lte', 'gte'.
        threshold: Threshold value.
        from_alias: Source alias to check (default 'Staging').
        to_alias: Target alias to promote to (default 'Production').
        dry_run: If True, show what would happen without promoting.
    """
    ops = {
        "lt": lambda v, t: v < t,
        "gt": lambda v, t: v > t,
        "lte": lambda v, t: v <= t,
        "gte": lambda v, t: v >= t,
    }
    if operator not in ops:
        return f"Invalid operator '{operator}'. Use: lt, gt, lte, gte"

    rm_data, err = _http.request_json(
        "mlflow", "GET",
        f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/registered-models/get"
        f"?name={urllib.parse.quote(model_name)}",
    )
    if err:
        return err
    aliases = {a["alias"]: a["version"] for a in rm_data.get("registered_model", {}).get("aliases", [])}
    version = aliases.get(from_alias)
    if not version:
        return f"No version under alias '{from_alias}' for {model_name}"

    ver_data, err = _http.request_json(
        "mlflow", "GET",
        f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/model-versions/get"
        f"?name={urllib.parse.quote(model_name)}&version={version}",
    )
    if err:
        return err
    run_id = ver_data["model_version"]["run_id"]

    run_data, err = _http.request_json(
        "mlflow", "GET",
        f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/runs/get?run_id={run_id}",
    )
    if err:
        return err
    metrics = run_data["run"]["data"].get("metrics", {})
    metric_val = metrics.get(metric)
    if metric_val is None:
        return f"Metric '{metric}' not in run {run_id}. Available: {list(metrics.keys())}"

    passes = ops[operator](metric_val, threshold)
    op_sym = {"lt": "<", "gt": ">", "lte": "<=", "gte": ">="}.get(operator, operator)
    summary = f"{metric}={metric_val:.4f} {op_sym} {threshold}"

    if dry_run:
        verdict = "WOULD promote" if passes else "would NOT promote"
        return f"[DRY RUN] {model_name} v{version}: {summary} → {verdict} to {to_alias}"

    if not passes:
        return f"Not promoted: {model_name} v{version}: {summary} (threshold not met)"

    _, err = _http.request_json(
        "mlflow", "POST",
        f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/registered-models/alias",
        json={"name": model_name, "alias": to_alias, "version": version},
    )
    if err:
        return f"Promotion failed: {err}"

    if _DB_OK:
        write_audit_event("agent", "agent", "alias_promoted", model_name,
                          {"from": from_alias, "to": to_alias, "version": version, metric: metric_val})

    return f"Promoted {model_name} v{version} → {to_alias}  ({summary})"


@tool
def get_input_drift_status(model_name: str = "") -> str:
    """Show embedding input distribution drift for all models or one model.

    Compares current rolling embedding statistics (norm, mean, std) against the
    stored baseline. Returns OK / WARNING / CRITICAL status per model.

    Args:
        model_name: Filter to a specific model (empty = all models).
    """
    if not _DB_OK:
        return _db_unavailable()
    with get_db() as conn:
        if model_name:
            models_list = [model_name]
        else:
            rows_m = conn.execute("SELECT DISTINCT model FROM input_snapshots").fetchall()
            models_list = [r["model"] for r in rows_m]

    if not models_list:
        return "No input snapshots recorded yet. Run the bridge to collect embedding data."

    lines = []
    for model in models_list:
        with get_db() as conn:
            snaps = conn.execute(
                "SELECT emb_norm, emb_mean, emb_std FROM input_snapshots WHERE model=? "
                "ORDER BY ts DESC LIMIT 200",
                (model,),
            ).fetchall()
        if not snaps:
            continue
        norms = [r["emb_norm"] for r in snaps]
        means = [r["emb_mean"] for r in snaps]
        stds_list = [r["emb_std"] for r in snaps]
        live = {
            "norm_mean": sum(norms) / len(norms),
            "mean_mean": sum(means) / len(means),
            "std_mean": sum(stds_list) / len(stds_list),
        }
        baseline = get_input_baseline(model)
        if baseline is None:
            max_z, status = 0.0, "OK (no baseline)"
        else:
            zs = []
            for metric in ("norm_mean", "mean_mean", "std_mean"):
                bstd = baseline.get(f"{metric}_std", 0.0)
                if bstd > 0:
                    zs.append(abs(live[metric] - baseline[metric]) / bstd)
            max_z = max(zs) if zs else 0.0
            status = "CRITICAL" if max_z >= 3.0 else "WARNING" if max_z >= 2.0 else "OK"
        lines.append(
            f"- {model}: norm_μ={live['norm_mean']:.3f}  emb_μ={live['mean_mean']:.4f}  "
            f"max_z={max_z:.2f} → {status}  (n={len(snaps)})"
        )
    return "\n".join(lines) if lines else "No input data found."


def _trigger_auto_retrain_summary(model_name: str = "") -> str:
    return f"Trigger auto-retrain check for {'all models' if not model_name else model_name}"


@tool
@confirmed_write(_trigger_auto_retrain_summary)
def trigger_auto_retrain(model_name: str = "") -> str:
    """Check drift z-scores and fire POST /retrain for models above their auto-retrain threshold.

    Respects the cooldown period. Models without auto-retrain config enabled are skipped.

    Args:
        model_name: Check only this model (empty = check all models with auto-retrain enabled).
    """
    if not _DB_OK:
        return _db_unavailable()
    import datetime

    configs = list_drift_auto_retrain()
    if model_name:
        configs = [c for c in configs if c["model"] == model_name]
    enabled = {c["model"]: c for c in configs if c["enabled"]}
    if not enabled:
        return "No models with auto-retrain enabled."

    with get_db() as conn:
        snap_rows = conn.execute(
            "SELECT model, prediction FROM drift_snapshots ORDER BY ts DESC LIMIT 5000"
        ).fetchall()

    grouped: dict[str, list[float]] = defaultdict(list)
    for r in snap_rows:
        grouped[r["model"]].append(r["prediction"])

    triggered, skipped = [], []
    for mdl, ar in enabled.items():
        preds = grouped.get(mdl, [])[:100]
        if not preds:
            skipped.append(f"{mdl}: no snapshots")
            continue
        n = len(preds)
        mean = sum(preds) / n
        baseline = get_drift_baseline(mdl)
        if baseline is None or baseline.get("std", 0) == 0:
            skipped.append(f"{mdl}: no baseline")
            continue
        z = abs(mean - baseline["mean"]) / baseline["std"]
        if z < ar["min_z_score"]:
            skipped.append(f"{mdl}: z={z:.2f} < {ar['min_z_score']}")
            continue
        if ar["last_triggered"]:
            last = datetime.datetime.fromisoformat(ar["last_triggered"])
            elapsed = (datetime.datetime.utcnow() - last).total_seconds()
            if elapsed < ar["cooldown_s"]:
                skipped.append(f"{mdl}: cooldown {elapsed:.0f}/{ar['cooldown_s']}s")
                continue
        data, err = _http.request_json(
            "control_plane", "POST",
            f"{config.CONTROL_PLANE_URL}/retrain",
            json={"model_name": mdl, "dataset_name": ar["dataset_name"], "is_dummy": False},
            headers={"Authorization": f"Bearer {config.CONTROL_PLANE_TOKEN}"},
        )
        if err:
            skipped.append(f"{mdl}: retrain POST failed — {err}")
        else:
            record_drift_trigger(mdl)
            write_audit_event("agent", "agent", "drift_auto_retrain_triggered", mdl,
                              {"z_score": z, "flow_run_id": data.get("flow_run_id")})
            triggered.append(f"{mdl}: z={z:.2f} → flow_run_id={data.get('flow_run_id')}")

    lines = []
    if triggered:
        lines.append(f"Triggered ({len(triggered)}): " + "; ".join(triggered))
    if skipped:
        lines.append(f"Skipped ({len(skipped)}): " + "; ".join(skipped))
    return "\n".join(lines) if lines else "Nothing to trigger."


@tool
def validate_model_serving(model_name: str, alias: str = "Staging", max_latency_s: float = 2.0) -> str:
    """Smoke-test a model on Ray Serve: check it responds and meets latency SLA.

    Sends 3 dummy requests and reports average/max latency vs the threshold.
    Returns PASS or FAIL with details.

    Args:
        model_name: Model name (e.g. 'JPCP').
        alias: MLflow alias to test (default 'Staging').
        max_latency_s: Maximum acceptable average latency in seconds (default 2.0).
    """
    import time

    dummy_body = {
        "embedding": [0.1] * 384,
        "num_nodes": 4,
        "user_id": "agent-validate",
        "model_name": model_name,
        "alias": alias,
    }
    latencies = []
    errors = []
    for _ in range(3):
        t0 = time.perf_counter()
        _, err = _http.request_json(
            "ray_serve", "POST",
            f"{config.RAY_SERVE_URL}/infer-pipeline/infer",
            json=dummy_body,
        )
        if err:
            errors.append(err)
        else:
            latencies.append(time.perf_counter() - t0)

    if errors:
        return f"FAIL — {len(errors)}/3 requests failed: {errors[0]}"

    avg = sum(latencies) / len(latencies)
    mx = max(latencies)
    passed = avg <= max_latency_s
    verdict = "PASS" if passed else "FAIL"
    return (
        f"{verdict} — {model_name} ({alias}): avg={avg:.3f}s  max={mx:.3f}s  "
        f"threshold={max_latency_s}s"
    )


# ── Feature 8: Quick platform summary ────────────────────────────────────────


@tool
def get_platform_summary() -> str:
    """Return a concise platform health snapshot in one call.

    Combines service reachability, registered model count, and pending approval
    count. Use this before deciding what to investigate in depth.
    """
    # Service health
    health_checks = {
        "control_plane": f"{config.CONTROL_PLANE_URL}/health",
        "ray_serve": f"{config.RAY_SERVE_URL}/health",
        "mlflow": f"{config.MLFLOW_URL}/health",
    }
    statuses: dict[str, str] = {}
    for svc, url in health_checks.items():
        _, err = _http.request_json(svc, "GET", url)
        statuses[svc] = "UP" if not err else "DOWN"

    # Registered model count
    models_data, err = _http.request_json(
        "mlflow", "GET",
        f"{config.MLFLOW_URL}/ajax-api/2.0/mlflow/registered-models/list",
    )
    model_count: int | str = len(models_data.get("registered_models", [])) if not err else "?"

    # Pending approvals
    approvals_data, err = _http.request_json(
        "control_plane", "GET",
        f"{config.CONTROL_PLANE_URL}/approvals",
    )
    if not err and isinstance(approvals_data, list):
        pending_count: int | str = sum(1 for a in approvals_data if a.get("status") == "pending")
    else:
        pending_count = "?"

    health_str = "\n".join(f"  {k}: {v}" for k, v in statuses.items())
    return (
        f"Platform Quick-Status:\n"
        f"  Registered models: {model_count}\n"
        f"  Pending approvals: {pending_count}\n"
        f"  Services:\n{health_str}"
    )


# ── Feature 9: Comprehensive platform diagnostic ──────────────────────────────


@tool
def diagnose_platform() -> str:
    """Run a full platform diagnostic and return prioritised findings.

    Checks service reachability, prediction drift (CRITICAL/WARNING),
    recent audit-log failures, and pending approval backlog. Returns a
    bullet-point list sorted by severity — use it as a first step when
    investigating issues or before writing a status report.
    """
    findings: list[str] = []

    # Service reachability
    for svc, url in {
        "control_plane": f"{config.CONTROL_PLANE_URL}/health",
        "ray_serve": f"{config.RAY_SERVE_URL}/health",
        "mlflow": f"{config.MLFLOW_URL}/health",
    }.items():
        _, err = _http.request_json(svc, "GET", url)
        if err:
            findings.append(f"CRITICAL: {svc} unreachable")

    # Drift anomalies
    if _DB_OK:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT model, prediction FROM drift_snapshots ORDER BY ts DESC LIMIT 500"
            ).fetchall()
        if rows:
            grouped: dict[str, list[float]] = defaultdict(list)
            for r in rows:
                grouped[r["model"]].append(r["prediction"])
            for model, preds in grouped.items():
                n = len(preds)
                mean = sum(preds) / n
                std = math.sqrt(sum((p - mean) ** 2 for p in preds) / n)
                baseline = get_drift_baseline(model)
                if baseline and baseline["std"] > 0:
                    z = abs(mean - baseline["mean"]) / baseline["std"]
                    if z >= 3.0:
                        findings.append(f"CRITICAL: {model} prediction drift z={z:.2f} (≥3σ)")
                    elif z >= 2.0:
                        findings.append(f"WARNING: {model} prediction drift z={z:.2f} (≥2σ)")

    # Recent audit failures (last 24 h)
    if _DB_OK:
        since = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            err_rows = conn.execute(
                "SELECT action, target FROM audit_events "
                "WHERE ts >= ? AND (action LIKE '%fail%' OR action LIKE '%error%') LIMIT 5",
                (since,),
            ).fetchall()
        for r in err_rows:
            findings.append(f"WARNING: recent failure — {r['action']} on {r['target']}")

    # Pending approval queue
    ap_data, ap_err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/approvals"
    )
    if not ap_err and isinstance(ap_data, list):
        pending = [a for a in ap_data if a.get("status") == "pending"]
        if len(pending) > 3:
            findings.append(f"INFO: {len(pending)} models pending operator approval")

    if not findings:
        findings.append("OK: No issues detected")

    return "## Platform Diagnostic\n" + "\n".join(f"- {f}" for f in findings)


TOOLS = [
    compare_model_versions,
    get_model_lineage,
    get_drift_status,
    get_input_drift_status,
    query_audit_log,
    set_traffic_split,
    promote_model,
    trigger_auto_retrain,
    validate_model_serving,
    get_platform_summary,   # Feature 8
    diagnose_platform,      # Feature 9
]
