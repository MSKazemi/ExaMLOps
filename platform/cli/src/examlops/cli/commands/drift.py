from __future__ import annotations

import math
import os

import typer

from examlops.cli import _output
from examlops.platform_db import (
    get_db,
    get_drift_auto_retrain,
    get_drift_baseline,
    get_input_baseline,
    init_db,
    list_drift_auto_retrain,
    record_drift_trigger,
    set_drift_auto_retrain,
    set_drift_baseline,
    set_input_baseline,
    write_audit_event,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_SNAPSHOT_WINDOW = 100
_BASELINE_WINDOW = 500
_WARN_Z = 2.0
_CRIT_Z = 3.0
_TREND_POINTS = 24  # recent-prediction points rendered as a sparkline in `drift status`

_EXAMPLES_STATUS = (
    "Examples:\n\n  exa drift status\n\n  exa drift status JPCP\n\n  exa --json drift status"
)
_EXAMPLES_BASELINE = "Examples:\n\n  exa drift baseline JPCP"
_EXAMPLES_RESET = "Examples:\n\n  exa drift reset JPCP"


def _compute_stats(values: list[float]) -> dict[str, float]:
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(variance)
    return {"mean": mean, "std": std, "n": float(n)}


def _drift_rows(model_filter: str | None) -> list[dict]:
    init_db()
    with get_db() as conn:
        if model_filter:
            models_list = [model_filter]
        else:
            models_rows = conn.execute("SELECT DISTINCT model FROM drift_snapshots").fetchall()
            models_list = [r["model"] for r in models_rows]

    results = []
    for model in models_list:
        with get_db() as conn:
            snap_rows = conn.execute(
                f"SELECT prediction FROM drift_snapshots WHERE model=? "
                f"ORDER BY ts DESC, rowid DESC LIMIT {_SNAPSHOT_WINDOW}",
                (model,),
            ).fetchall()
        preds = [r["prediction"] for r in snap_rows]
        if not preds:
            continue
        live = _compute_stats(preds)
        baseline = get_drift_baseline(model)
        if baseline is None:
            z = 0.0
            status = "OK (no baseline)"
        elif baseline["std"] == 0:
            z = 0.0
            status = "OK (no baseline)"
        else:
            z = abs(live["mean"] - baseline["mean"]) / baseline["std"]
            if z >= _CRIT_Z:
                status = "CRITICAL"
            elif z >= _WARN_Z:
                status = "WARNING"
            else:
                status = "OK"
        # preds are newest-first; reverse to oldest→newest so the trend reads left-to-right.
        recent = [round(float(p), 3) for p in reversed(preds)][-_TREND_POINTS:]
        results.append(
            {
                "model": model,
                "live_mean": round(live["mean"], 3),
                "live_std": round(live["std"], 3),
                "baseline_mean": round(baseline["mean"], 3) if baseline else None,
                "z_score": round(z, 2),
                "status": status,
                "n_snapshots": len(preds),
                "recent": recent,
            }
        )
    return results


@app.command(epilog=_EXAMPLES_STATUS)
def status(
    model: str | None = typer.Argument(None, help="Model name filter (default: all models)"),
    watch: bool = typer.Option(
        False, "--watch", "-w", help="Live auto-refreshing view (Ctrl-C to exit)"
    ),
    interval: int = typer.Option(5, "--interval", help="Refresh interval in seconds for --watch"),
):
    """Show prediction drift status for all models (or one model)."""
    if watch and not _output.json_mode:
        _output.watch_loop(lambda: _render_drift_status(model), interval)
        return
    _render_drift_status(model)


def _render_drift_status(model: str | None) -> None:
    rows = _drift_rows(model)
    if not rows:
        _output.ok("No data — run exa drift baseline <MODEL> after collecting some predictions")
        return
    if _output.json_mode:
        _output.print_json(rows)
        return
    # Live σ is intentionally omitted from the table (the Trend sparkline conveys variability)
    # to keep it readable at 80 columns; the full value stays in `--json` output.
    cols = ["Model", "Live μ", "Baseline μ", "z-score", "Status", "Snapshots", "Trend"]
    table_rows = [
        [
            r["model"],
            f"{r['live_mean']:.3f}",
            f"{r['baseline_mean']:.3f}" if r["baseline_mean"] is not None else "—",
            f"{r['z_score']:.2f}",
            r["status"],
            str(r["n_snapshots"]),
            _output.sparkline(r["recent"]) or "—",
        ]
        for r in rows
    ]
    _output.print_table("Drift Status", cols, table_rows)


@app.command(epilog=_EXAMPLES_BASELINE)
def baseline(
    model: str = typer.Argument(..., help="Model name to set baseline for"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the baseline that would be set without writing it"
    ),
):
    """Store current rolling stats as the drift baseline for a model."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT prediction FROM drift_snapshots WHERE model=? "
            f"ORDER BY ts DESC, rowid DESC LIMIT {_BASELINE_WINDOW}",
            (model,),
        ).fetchall()
    preds = [r["prediction"] for r in rows]
    if len(preds) < 10:
        _output.error(f"Need at least 10 snapshots, have {len(preds)}. Run the bridge first.")
        return
    stats = _compute_stats(preds)

    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "model": model, "would_set": stats})
        else:
            _output.info(f"Dry run — baseline for {model} would be set to:")
            _output.print_record({k: round(v, 3) for k, v in stats.items()})
        return

    prev = get_drift_baseline(model)
    if prev is not None and not _output.confirm(
        f"Overwrite existing drift baseline for {model}?", default=True
    ):
        _output.warning("Aborted — baseline unchanged.")
        raise typer.Exit(0)

    set_drift_baseline(model, stats)
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event("cli", actor, "drift_baseline_set", model, stats)
    _output.ok(
        f"Baseline set for {model}: mean={stats['mean']:.3f}  "
        f"std={stats['std']:.3f}  n={int(stats['n'])}"
    )


@app.command(epilog=_EXAMPLES_RESET)
def reset(
    model: str = typer.Argument(..., help="Model name to clear snapshots for"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show how many snapshots would be cleared without deleting them"
    ),
):
    """Clear all drift snapshots for a model (keeps baseline)."""
    init_db()
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM drift_snapshots WHERE model=?", (model,)
        ).fetchone()["c"]

    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "model": model, "would_clear": n})
        else:
            _output.info(f"Dry run — would clear {n} drift snapshot(s) for {model}.")
        return

    if n == 0:
        _output.ok(f"No drift snapshots to clear for {model}")
        return
    if not _output.confirm(
        f"Delete {n} drift snapshot(s) for {model}? This cannot be undone.", default=False
    ):
        _output.warning("Aborted — snapshots unchanged.")
        raise typer.Exit(0)

    with get_db() as conn:
        conn.execute("DELETE FROM drift_snapshots WHERE model=?", (model,))
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event("cli", actor, "drift_reset", model, {"cleared": n})
    _output.ok(f"Cleared {n} drift snapshot(s) for {model}")


# ---------------------------------------------------------------------------
# auto-retrain sub-group
# ---------------------------------------------------------------------------

auto_retrain_app = typer.Typer(
    no_args_is_help=True, context_settings={"help_option_names": ["-h", "--help"]}
)
app.add_typer(auto_retrain_app, name="auto-retrain")

_EXAMPLES_AR_ENABLE = (
    "Examples:\n\n"
    "  exa drift auto-retrain enable JPCP --dataset PM100Dataset\n\n"
    "  exa drift auto-retrain enable JPCP --min-z 2.5 --cooldown 1800"
)
_EXAMPLES_AR_DISABLE = "Examples:\n\n  exa drift auto-retrain disable JPCP"
_EXAMPLES_AR_STATUS = "Examples:\n\n  exa drift auto-retrain status"
_EXAMPLES_TRIGGER = (
    "Examples:\n\n"
    "  exa drift trigger              # fire retrains for CRITICAL models\n\n"
    "  exa drift trigger --dry-run    # preview without firing"
)


@auto_retrain_app.command("enable", epilog=_EXAMPLES_AR_ENABLE)
def auto_retrain_enable(
    model: str = typer.Argument(..., help="Model name"),
    dataset: str = typer.Option("PM100Dataset", "--dataset", "-d", help="Dataset class name"),
    min_z: float = typer.Option(3.0, "--min-z", help="Z-score threshold to trigger retrain"),
    cooldown: int = typer.Option(3600, "--cooldown", help="Seconds between triggers"),
):
    """Enable drift-triggered auto-retrain for a model."""
    init_db()
    set_drift_auto_retrain(
        model, enabled=True, min_z_score=min_z, dataset_name=dataset, cooldown_s=cooldown
    )
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event(
        "cli",
        actor,
        "drift_auto_retrain_enabled",
        model,
        {"min_z": min_z, "dataset": dataset, "cooldown_s": cooldown},
    )
    _output.ok(
        f"Auto-retrain enabled for {model} (z≥{min_z}, dataset={dataset}, cooldown={cooldown}s)"
    )


@auto_retrain_app.command("disable", epilog=_EXAMPLES_AR_DISABLE)
def auto_retrain_disable(
    model: str = typer.Argument(..., help="Model name"),
):
    """Disable drift-triggered auto-retrain for a model."""
    init_db()
    cfg = get_drift_auto_retrain(model)
    if cfg is None:
        _output.error(f"No auto-retrain config found for {model}")
        return
    set_drift_auto_retrain(
        model,
        enabled=False,
        min_z_score=cfg["min_z_score"],
        dataset_name=cfg["dataset_name"],
        cooldown_s=cfg["cooldown_s"],
    )
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event("cli", actor, "drift_auto_retrain_disabled", model, {})
    _output.ok(f"Auto-retrain disabled for {model}")


@auto_retrain_app.command("status", epilog=_EXAMPLES_AR_STATUS)
def auto_retrain_status():
    """Show auto-retrain config for all models."""
    init_db()
    rows = list_drift_auto_retrain()
    if not rows:
        _output.ok("No auto-retrain config — use: exa drift auto-retrain enable <MODEL>")
        return
    if _output.json_mode:
        _output.print_json(rows)
        return
    cols = ["Model", "Enabled", "Min Z", "Dataset", "Cooldown (s)", "Last Triggered"]
    table_rows = [
        [
            r["model"],
            "yes" if r["enabled"] else "no",
            str(r["min_z_score"]),
            r["dataset_name"],
            str(r["cooldown_s"]),
            r["last_triggered"] or "—",
        ]
        for r in rows
    ]
    _output.print_table("Drift Auto-Retrain Config", cols, table_rows)


# ---------------------------------------------------------------------------
# trigger command (top-level on drift app)
# ---------------------------------------------------------------------------


@app.command(epilog=_EXAMPLES_TRIGGER)
def trigger(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be triggered without firing"
    ),
):
    """Check drift z-scores and fire POST /retrain for models above threshold."""
    import datetime

    from examlops.cli._client import ClientError, post
    from examlops.cli._config import load_config

    init_db()
    configs = list_drift_auto_retrain()
    enabled_configs = {c["model"]: c for c in configs if c["enabled"]}
    if not enabled_configs:
        _output.ok(
            "No models with auto-retrain enabled — use: exa drift auto-retrain enable <MODEL>"
        )
        return

    drift_rows = _drift_rows(None)
    cfg = load_config()
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    triggered = []
    skipped = []

    for row in drift_rows:
        model = row["model"]
        if model not in enabled_configs:
            continue
        ar = enabled_configs[model]
        z = row["z_score"]
        if z < ar["min_z_score"]:
            skipped.append({"model": model, "reason": f"z={z:.2f} < threshold {ar['min_z_score']}"})
            continue
        if ar["last_triggered"]:
            last = datetime.datetime.fromisoformat(ar["last_triggered"])
            elapsed = (datetime.datetime.utcnow() - last).total_seconds()
            if elapsed < ar["cooldown_s"]:
                skipped.append(
                    {"model": model, "reason": f"cooldown {elapsed:.0f}/{ar['cooldown_s']}s"}
                )
                continue
        if dry_run:
            triggered.append({"model": model, "z": z, "action": "would retrain"})
            continue
        body = {"model_name": model, "dataset_name": ar["dataset_name"], "is_dummy": False}
        try:
            result = post(f"{cfg.control_plane_url}/retrain", body, token=cfg.control_plane_token)
            record_drift_trigger(model)
            write_audit_event(
                "cli",
                actor,
                "drift_auto_retrain_triggered",
                model,
                {"z_score": z, "flow_run_id": result.get("flow_run_id")},
            )
            triggered.append({"model": model, "z": z, "flow_run_id": result.get("flow_run_id")})
        except ClientError as e:
            _output.error(f"Failed to trigger retrain for {model}: {e}")

    if _output.json_mode:
        _output.print_json({"triggered": triggered, "skipped": skipped})
        return
    if triggered:
        cols = ["Model", "Z-Score", "Flow Run ID"]
        _output.print_table(
            "Triggered Retrains" + (" (dry-run)" if dry_run else ""),
            cols,
            [[t["model"], f"{t['z']:.2f}", t.get("flow_run_id") or "—"] for t in triggered],
        )
    if skipped:
        cols = ["Model", "Reason"]
        _output.print_table("Skipped", cols, [[s["model"], s["reason"]] for s in skipped])
    if not triggered and not skipped:
        _output.ok("All enabled models below drift threshold — no retrains triggered")


# ---------------------------------------------------------------------------
# input drift sub-group (embedding distribution monitoring)
# ---------------------------------------------------------------------------

input_app = typer.Typer(
    no_args_is_help=True, context_settings={"help_option_names": ["-h", "--help"]}
)
app.add_typer(input_app, name="input")

_INPUT_WINDOW = 200
_INPUT_BASELINE_WINDOW = 1000

_EXAMPLES_INPUT_STATUS = (
    "Examples:\n\n"
    "  exa drift input status\n\n"
    "  exa drift input status JPCP\n\n"
    "  exa --json drift input status"
)
_EXAMPLES_INPUT_BASELINE = "Examples:\n\n  exa drift input baseline JPCP"


def _input_drift_rows(model_filter: str | None) -> list[dict]:
    init_db()
    with get_db() as conn:
        if model_filter:
            models_list = [model_filter]
        else:
            rows = conn.execute("SELECT DISTINCT model FROM input_snapshots").fetchall()
            models_list = [r["model"] for r in rows]

    results = []
    for model in models_list:
        with get_db() as conn:
            snap_rows = conn.execute(
                f"SELECT emb_norm, emb_mean, emb_std FROM input_snapshots WHERE model=? "
                f"ORDER BY ts DESC LIMIT {_INPUT_WINDOW}",
                (model,),
            ).fetchall()
        if not snap_rows:
            continue
        norms = [r["emb_norm"] for r in snap_rows]
        means = [r["emb_mean"] for r in snap_rows]
        stds = [r["emb_std"] for r in snap_rows]
        live = {
            "norm_mean": sum(norms) / len(norms),
            "mean_mean": sum(means) / len(means),
            "std_mean": sum(stds) / len(stds),
        }
        baseline = get_input_baseline(model)
        if baseline is None:
            status = "OK (no baseline)"
            max_z = 0.0
        else:
            zs = []
            for metric in ("norm_mean", "mean_mean", "std_mean"):
                bstd = baseline.get(f"{metric}_std", 0.0)
                if bstd > 0:
                    zs.append(abs(live[metric] - baseline[metric]) / bstd)
            max_z = max(zs) if zs else 0.0
            if max_z >= _CRIT_Z:
                status = "CRITICAL"
            elif max_z >= _WARN_Z:
                status = "WARNING"
            else:
                status = "OK"
        results.append(
            {
                "model": model,
                "live_norm_mean": round(live["norm_mean"], 3),
                "live_emb_mean": round(live["mean_mean"], 4),
                "live_emb_std": round(live["std_mean"], 4),
                "max_z": round(max_z, 2),
                "status": status,
                "n_snapshots": len(snap_rows),
            }
        )
    return results


@input_app.command("status", epilog=_EXAMPLES_INPUT_STATUS)
def input_status(
    model: str | None = typer.Argument(None, help="Model name filter (default: all models)"),
):
    """Show input embedding distribution drift for all models (or one model)."""
    rows = _input_drift_rows(model)
    if not rows:
        _output.ok("No input data — bridge must be running to collect embedding snapshots")
        return
    if _output.json_mode:
        _output.print_json(rows)
        return
    cols = ["Model", "Norm μ", "Emb μ", "Emb σ", "Max Z", "Status", "Snapshots"]
    table_rows = [
        [
            r["model"],
            f"{r['live_norm_mean']:.3f}",
            f"{r['live_emb_mean']:.4f}",
            f"{r['live_emb_std']:.4f}",
            f"{r['max_z']:.2f}",
            r["status"],
            str(r["n_snapshots"]),
        ]
        for r in rows
    ]
    _output.print_table("Input Drift Status", cols, table_rows)


_EXAMPLES_SNAPSHOTS = (
    "Examples:\n\n"
    "  exa drift snapshots JPCP\n\n"
    "  exa drift snapshots JPCP --last 50\n\n"
    "  exa --json drift snapshots JPCP --raw"
)


@app.command("snapshots", epilog=_EXAMPLES_SNAPSHOTS)
def snapshots(
    model: str = typer.Argument(..., help="Model name"),
    last: int = typer.Option(100, "--last", "-n", help="Number of recent snapshots"),
    raw: bool = typer.Option(False, "--raw", help="Show all columns including job_id"),
):
    """Show raw prediction drift snapshots for a model."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, ts, model, alias, prediction, job_id FROM drift_snapshots "
            "WHERE model=? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (model, max(1, last)),
        ).fetchall()
    if not rows:
        _output.ok(f"No snapshots found for {model}")
        return
    data = [
        {
            "id": r["id"],
            "ts": r["ts"],
            "model": r["model"],
            "alias": r["alias"],
            "prediction": r["prediction"],
            "job_id": r["job_id"],
        }
        for r in rows
    ]
    if _output.json_mode:
        _output.print_json(data)
        return
    if raw:
        cols = ["ID", "Time", "Model", "Alias", "Prediction", "Job ID"]
        table_rows = [
            [
                str(r["id"]),
                r["ts"][:19],
                r["model"],
                r["alias"],
                f"{r['prediction']:.4f}",
                r["job_id"] or "—",
            ]
            for r in data
        ]
    else:
        cols = ["Time", "Alias", "Prediction"]
        table_rows = [[r["ts"][:19], r["alias"], f"{r['prediction']:.4f}"] for r in data]
    _output.print_table(f"Drift Snapshots — {model} (last {len(data)})", cols, table_rows)


@input_app.command("baseline", epilog=_EXAMPLES_INPUT_BASELINE)
def input_baseline(
    model: str = typer.Argument(..., help="Model name to set input baseline for"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the input baseline that would be set without writing it"
    ),
):
    """Store current rolling embedding statistics as the input drift baseline."""
    init_db()
    with get_db() as conn:
        snap_rows = conn.execute(
            f"SELECT emb_norm, emb_mean, emb_std FROM input_snapshots WHERE model=? "
            f"ORDER BY ts DESC LIMIT {_INPUT_BASELINE_WINDOW}",
            (model,),
        ).fetchall()
    if len(snap_rows) < 10:
        _output.error(
            f"Need at least 10 input snapshots, have {len(snap_rows)}. Run the bridge first."
        )
        return

    def _stats(values: list[float]) -> tuple[float, float]:
        n = len(values)
        mean = sum(values) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
        return mean, std

    norms = [r["emb_norm"] for r in snap_rows]
    emb_means = [r["emb_mean"] for r in snap_rows]
    emb_stds = [r["emb_std"] for r in snap_rows]
    norm_mean, norm_std = _stats(norms)
    mean_mean, mean_std = _stats(emb_means)
    std_mean, std_std = _stats(emb_stds)

    stats = {
        "norm_mean": norm_mean,
        "norm_mean_std": norm_std,
        "mean_mean": mean_mean,
        "mean_mean_std": mean_std,
        "std_mean": std_mean,
        "std_mean_std": std_std,
        "n": float(len(snap_rows)),
    }
    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "model": model, "would_set": stats})
        else:
            _output.info(
                f"Dry run — input baseline for {model} would be: norm_μ={norm_mean:.3f}  "
                f"emb_μ={mean_mean:.4f}  emb_σ={std_mean:.4f}  n={len(snap_rows)}"
            )
        return

    if get_input_baseline(model) is not None and not _output.confirm(
        f"Overwrite existing input drift baseline for {model}?", default=True
    ):
        _output.warning("Aborted — input baseline unchanged.")
        raise typer.Exit(0)

    set_input_baseline(model, stats)
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event("cli", actor, "input_baseline_set", model, {"n": len(snap_rows)})
    _output.ok(
        f"Input baseline set for {model}: norm_μ={norm_mean:.3f}  emb_μ={mean_mean:.4f}  "
        f"emb_σ={std_mean:.4f}  n={len(snap_rows)}"
    )


_EXAMPLES_INPUT_RESET = "Examples:\n\n  exa drift input reset JPCP"


@input_app.command("reset", epilog=_EXAMPLES_INPUT_RESET)
def input_reset(
    model: str = typer.Argument(..., help="Model name to clear input snapshots for"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show how many snapshots would be cleared without deleting them"
    ),
):
    """Clear all input embedding snapshots for a model (keeps baseline)."""
    init_db()
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM input_snapshots WHERE model=?", (model,)
        ).fetchone()["c"]

    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "model": model, "would_clear": n})
        else:
            _output.info(f"Dry run — would clear {n} input snapshot(s) for {model}.")
        return

    if n == 0:
        _output.ok(f"No input snapshots to clear for {model}")
        return
    if not _output.confirm(
        f"Delete {n} input snapshot(s) for {model}? This cannot be undone.", default=False
    ):
        _output.warning("Aborted — snapshots unchanged.")
        raise typer.Exit(0)

    with get_db() as conn:
        conn.execute("DELETE FROM input_snapshots WHERE model=?", (model,))
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event("cli", actor, "input_reset", model, {"cleared": n})
    _output.ok(f"Cleared {n} input snapshot(s) for {model}")
