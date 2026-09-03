from __future__ import annotations

import math
import os

import typer

from examlops.cli import _output
from examlops.cli._help import make_ordered_group
from examlops.cli._provenance import audit_details, reason_option
from examlops.data import get_db, init_db
from examlops.data.audit import write_audit_event
from examlops.data.drift import (
    claim_drift_trigger,
    get_drift_auto_retrain,
    get_drift_baseline,
    get_input_baseline,
    list_drift_auto_retrain,
    set_drift_auto_retrain,
    set_drift_baseline,
    set_input_baseline,
)
from examlops.drift_providers import resolve_drift_score_fn
from examlops.evidence import AUTONOMOUS, correlated
from examlops.rollback import AutonomousActionRefused, require_rollback

# Help panels for `exa drift` (auto-retrain and input sub-groups are added within this module).
_PANELS: list[tuple[str, list[str]]] = [
    ("Detection", ["status", "snapshots", "concept", "estimate", "profile", "forecast", "events"]),
    ("Baselines", ["baseline", "reset"]),
    ("Response", ["trigger", "auto-retrain", "input", "corruption"]),
]

app = typer.Typer(
    cls=make_ordered_group(_PANELS),
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
        _score = resolve_drift_score_fn()
        z, status = _score(live["mean"], live["std"], baseline)
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
    reason: str | None = reason_option(),
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
    write_audit_event("cli", actor, "drift_baseline_set", model, audit_details(stats, reason))
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
    reason: str | None = reason_option(),
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
    write_audit_event("cli", actor, "drift_reset", model, audit_details({"cleared": n}, reason))
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
    dataset: str | None = typer.Option(
        None, "--dataset", "-d", help="Dataset class name (default: model's primary dataset)"
    ),
    min_z: float = typer.Option(3.0, "--min-z", help="Z-score threshold to trigger retrain"),
    cooldown: int = typer.Option(3600, "--cooldown", help="Seconds between triggers"),
):
    """Enable drift-triggered auto-retrain for a model."""
    from examlops.usecase import default_dataset_for

    init_db()
    # No hardcoded dataset (ADR 0094): resolve the model's primary dataset from the pack YAML.
    dataset = dataset or default_dataset_for(model)
    if not dataset:
        _output.error(f"--dataset is required (no default dataset in {model}'s YAML)")
        raise typer.Exit(1)
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


def _alias_version(model: str, alias: str) -> str | None:
    """The version an alias currently points at — i.e. the one a rollback would restore."""
    try:
        import mlflow

        return str(mlflow.MlflowClient().get_model_version_by_alias(model.lower(), alias).version)
    except Exception:
        return None


def _declare_rollback(action: str, model: str, alias: str = "Production") -> str | None:
    """Record how an autonomous action would be undone, before it is taken (ADR 0113).

    The inverse is built from the alias's *current* version, read now: once a retrain has run and
    promoted, the version a rollback would restore is no longer the one the alias points at.
    """
    from examlops.evidence import with_rollback_ref
    from examlops.rollback import build_rollback_ref

    previous = _alias_version(model, alias)
    if previous is None:
        return None
    ref = build_rollback_ref(action, model=model, previous_version=previous, alias=alias)
    if ref:
        with_rollback_ref(ref)
    return ref


def _corruption_signal_or_none(model: str):
    """The corruption signal, or ``None`` if it cannot be computed.

    A detector that raises takes the retrain path down with it, so a failure here is
    reported and the caller falls back to the pre-ADR-0114 gates rather than blocking
    every model on a broken read. It is logged, not swallowed silently — a guard whose
    only success signal is silence is exactly what this platform keeps getting wrong.
    """
    from examlops.corruption import signal_for_model

    try:
        return signal_for_model(model)
    except Exception as exc:  # pragma: no cover - defensive
        _output.warning(f"corruption detection unavailable for {model}: {exc}")
        return None


def _classification_from_corruption(signal):
    from examlops.corruption import AnomalyClassification

    return AnomalyClassification(
        klass="suspected_hardware",
        reason="; ".join(signal.reasons) or "corruption signal positive",
        remediation="quarantine_node",
        autonomous_remediation_allowed=False,
        operator_event=True,
        evidence=signal.evidence,
    )


def _classify_or_none(model: str, drift_row: dict):
    """Classify one drifting model, or ``None`` when classification itself failed."""
    from examlops.corruption import classify_anomaly

    signal = _corruption_signal_or_none(model)
    if signal is None:
        return None
    try:
        rows = _input_drift_rows(model)
    except Exception as exc:  # pragma: no cover - defensive
        _output.warning(f"input-drift evidence unavailable for {model}: {exc}")
        return None
    return classify_anomaly(drift_row, signal, rows[0] if rows else None)


def _record_suppression(source: str, actor: str, model: str, score: float, classification) -> None:
    """Record a suppressed remediation in the evidence chain (ADR 0114 decision 4).

    A retrain that does not happen leaves no trace of its own, which is what would make
    this invisible; the audit event and the operator drift event are that trace, and they
    are also the denominator G4.11 needs.
    """
    from examlops.data.drift import record_drift_event

    detail = {"score": score, **classification.as_dict()}
    record_drift_event(
        model,
        "corruption",
        severity="CRITICAL" if classification.klass == "suspected_hardware" else "WARNING",
        score=score,
        metric="anomaly_class",
        detail=detail,
    )
    write_audit_event(source, actor, "drift_retrain_suppressed", model, detail)


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
    suppressed: list[dict] = []
    refused: list[dict] = []

    # ADR 0110: `drift trigger` fires retrains on the platform's own initiative, so everything it
    # writes — the retrains and the ADR-0114 suppressions alike — belongs to one correlated,
    # *autonomous* unit of work. Without the mode an auditor cannot tell these from a retrain a
    # person asked for, which is half of what the W2 gate asks.
    trigger_ctx = correlated(mode=AUTONOMOUS, on_behalf_of=actor)
    trigger_ctx.__enter__()

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
        # ADR 0114: classify before remediating. A z-score threshold and a cooldown were the
        # only gates here, and silent data corruption perturbs exactly the statistic the
        # z-score is computed from — so a hardware fault fired an autonomous retrain and
        # trained a model on corrupt data. Only `data_drift` may retrain autonomously.
        classification = _classify_or_none(model, row)
        if classification is not None and not classification.autonomous_remediation_allowed:
            _record_suppression("cli", actor, model, z, classification)
            suppressed.append(
                {
                    "model": model,
                    "class": classification.klass,
                    "reason": classification.reason,
                    "remediation": classification.remediation,
                }
            )
            continue

        # ADR 0113 decision 2: declare the inverse, then refuse if there is none. Evaluated
        # before the dry-run branch so a preview reports what the real run would do.
        _declare_rollback("drift_auto_retrain_triggered", model)
        try:
            require_rollback("drift_auto_retrain_triggered")
        except AutonomousActionRefused as exc:
            refused.append({"model": model, "action": "retrain", "reason": str(exc)})
            if not dry_run:
                write_audit_event(
                    "cli",
                    actor,
                    "autonomous_action_refused",
                    model,
                    {"attempted": "drift_auto_retrain_triggered", "reason": str(exc)},
                )
            continue

        if dry_run:
            triggered.append({"model": model, "z": z, "action": "would retrain"})
            continue
        # Atomic cooldown claim (stamps last_triggered) — closes the check-then-record TOCTOU so a
        # concurrent `drift trigger`/autopilot cycle cannot double-fire this model's retrain.
        if not claim_drift_trigger(model, ar["cooldown_s"]):
            skipped.append({"model": model, "reason": "cooldown active (claimed concurrently)"})
            continue
        body = {"model_name": model, "dataset_name": ar["dataset_name"], "is_dummy": False}
        try:
            result = post(f"{cfg.control_plane_url}/retrain", body, token=cfg.control_plane_token)
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

    # C5 (R2): concept-CRITICAL detections are also auto-retrain consumable, subject
    # to the same cooldown. Skip any model already triggered above on prediction drift.
    from examlops.data.drift import latest_drift_event

    already = {t["model"] for t in triggered}
    for model, ar in enabled_configs.items():
        if model in already:
            continue
        ev = latest_drift_event(model, "concept")
        if not ev or ev["severity"] != "CRITICAL":
            continue
        # The concept path is the second door into an autonomous retrain, so ADR 0114's
        # suppression has to hold here as well. Only the corruption axis is consulted:
        # a concept breach *is* an input→output relationship change, so input-drift
        # quietness does not carry the same meaning it does for prediction drift.
        corr_signal = _corruption_signal_or_none(model)
        if corr_signal is not None and corr_signal.suspected_sdc:
            cls = _classification_from_corruption(corr_signal)
            _record_suppression("cli", actor, model, float(ev.get("score") or 0.0), cls)
            suppressed.append(
                {
                    "model": model,
                    "class": cls.klass,
                    "reason": f"concept: {cls.reason}",
                    "remediation": cls.remediation,
                }
            )
            continue
        if ar["last_triggered"]:
            last = datetime.datetime.fromisoformat(ar["last_triggered"])
            if (datetime.datetime.utcnow() - last).total_seconds() < ar["cooldown_s"]:
                skipped.append({"model": model, "reason": "concept: cooldown active"})
                continue
        if dry_run:
            triggered.append(
                {"model": model, "z": ev.get("score") or 0.0, "action": "would retrain (concept)"}
            )
            continue
        if not claim_drift_trigger(model, ar["cooldown_s"]):
            skipped.append(
                {"model": model, "reason": "concept: cooldown active (claimed concurrently)"}
            )
            continue
        body = {"model_name": model, "dataset_name": ar["dataset_name"], "is_dummy": False}
        try:
            result = post(f"{cfg.control_plane_url}/retrain", body, token=cfg.control_plane_token)
            write_audit_event(
                "cli",
                actor,
                "drift_auto_retrain_triggered",
                model,
                {
                    "drift_kind": "concept",
                    "score": ev.get("score"),
                    "flow_run_id": result.get("flow_run_id"),
                },
            )
            triggered.append(
                {
                    "model": model,
                    "z": ev.get("score") or 0.0,
                    "flow_run_id": result.get("flow_run_id"),
                }
            )
        except ClientError as e:
            _output.error(f"Failed to trigger concept-drift retrain for {model}: {e}")

    trigger_ctx.__exit__(None, None, None)

    if _output.json_mode:
        _output.print_json(
            {
                "triggered": triggered,
                "skipped": skipped,
                "suppressed": suppressed,
                "refused": refused,
            }
        )
        return
    if triggered:
        cols = ["Model", "Z-Score", "Flow Run ID"]
        _output.print_table(
            "Triggered Retrains" + (" (dry-run)" if dry_run else ""),
            cols,
            [[t["model"], f"{t['z']:.2f}", t.get("flow_run_id") or "—"] for t in triggered],
        )
    if refused:
        _output.print_table(
            "Refused — no declared way to undo the action (ADR 0113)",
            ["Model", "Action", "Reason"],
            [[r["model"], r["action"], r["reason"]] for r in refused],
        )
    if suppressed:
        _output.print_table(
            "Suppressed — classification did not permit an autonomous retrain (ADR 0114)",
            ["Model", "Class", "Remediation", "Reason"],
            [[s["model"], s["class"], s["remediation"], s["reason"]] for s in suppressed],
        )
    if skipped:
        cols = ["Model", "Reason"]
        _output.print_table("Skipped", cols, [[s["model"], s["reason"]] for s in skipped])
    if not triggered and not skipped and not suppressed and not refused:
        _output.ok("All enabled models below drift threshold — no retrains triggered")


# ---------------------------------------------------------------------------
# corruption sub-group (ADR 0114 — is this the data, or the machine?)
# ---------------------------------------------------------------------------

corruption_app = typer.Typer(
    no_args_is_help=True, context_settings={"help_option_names": ["-h", "--help"]}
)
app.add_typer(corruption_app, name="corruption")

_CORRUPTION_BASELINE_WINDOW = 500

_EXAMPLES_CORRUPTION_STATUS = (
    "Examples:\n\n"
    "  exa drift corruption status\n\n"
    "  exa drift corruption status JPCP\n\n"
    "  exa --json drift corruption status"
)
_EXAMPLES_CORRUPTION_BASELINE = "Examples:\n\n  exa drift corruption baseline JPCP"
_EXAMPLES_CLASSIFY = (
    "Examples:\n\n  exa drift corruption classify\n\n  exa drift corruption classify JPCP"
)
_EXAMPLES_SELFTEST = "Examples:\n\n  exa drift corruption selftest JPCP"


def _corruption_models(model_filter: str | None) -> list[str]:
    init_db()
    if model_filter:
        return [model_filter]
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT model FROM drift_snapshots").fetchall()
    return [r["model"] for r in rows]


@corruption_app.command("status", epilog=_EXAMPLES_CORRUPTION_STATUS)
def corruption_status(
    model: str | None = typer.Argument(None, help="Model name filter (default: all models)"),
):
    """Show the corruption signal per model — NaN/Inf **and** unexpected zeros.

    A NaN/Inf guard alone sees about 1% of silent data corruption, so it is never reported
    on its own here (ADR 0114 decision 1).
    """
    from examlops.corruption import signal_for_model

    models_list = _corruption_models(model)
    rows = []
    for name in models_list:
        sig = signal_for_model(name)
        rows.append({"model": name, **sig.as_dict()})
    if not rows:
        _output.ok("No prediction snapshots — nothing to check for corruption")
        return
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Corruption Signal",
        ["Model", "NaN/Inf", "Zero Rate", "Baseline", "Shift", "Suspected SDC", "N"],
        [
            [
                r["model"],
                f"{r['nan_inf_rate']:.3f}",
                f"{r['zero_rate']:.3f}",
                "—" if r["zero_rate_baseline"] is None else f"{r['zero_rate_baseline']:.3f}",
                f"{r['distribution_shift']:.2f}",
                "YES" if r["suspected_sdc"] else "no",
                str(r["n"]),
            ]
            for r in rows
        ],
    )
    for r in rows:
        for reason in r["reasons"]:
            _output.detail(f"{r['model']}: {reason}")


@corruption_app.command("baseline", epilog=_EXAMPLES_CORRUPTION_BASELINE)
def corruption_baseline(
    model: str = typer.Argument(..., help="Model name"),
    reason: str | None = reason_option(),
):
    """Store the current zero-rate and spread as this model's corruption baseline.

    Zero rates drift legitimately (a genuinely sparser input distribution), so like
    ``exa drift baseline`` this is an explicit, audited act rather than a rolling window.
    """
    from examlops.corruption import corruption_stats
    from examlops.data.drift import set_corruption_baseline

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT prediction FROM drift_snapshots WHERE model=? ORDER BY ts DESC, rowid DESC "
            "LIMIT ?",
            (model, _CORRUPTION_BASELINE_WINDOW),
        ).fetchall()
    preds = [r["prediction"] for r in rows]
    if not preds:
        _output.error(f"No prediction snapshots for {model} — nothing to baseline")
    stats = corruption_stats(preds)
    set_corruption_baseline(model, stats)
    write_audit_event(
        "cli",
        os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown",
        "corruption_baseline_set",
        model,
        audit_details(dict(stats), reason),
    )
    if _output.json_mode:
        _output.print_json({"model": model, **stats})
        return
    _output.ok(
        f"Corruption baseline set for {model}: zero_rate={stats['zero_rate']:.4f} "
        f"std={stats['std']:.4g} over {int(stats['n'])} predictions"
    )


@corruption_app.command("classify", epilog=_EXAMPLES_CLASSIFY)
def corruption_classify(
    model: str | None = typer.Argument(None, help="Model name filter (default: all models)"),
):
    """Name the anomaly — data drift, hardware, regression, or undetermined.

    The remediation follows from the class, never from the z-score (ADR 0114 decision 2).
    """
    rows = _drift_rows(model)
    if not rows:
        _output.ok("No prediction snapshots — nothing to classify")
        return
    out = []
    for row in rows:
        cls = _classify_or_none(row["model"], row)
        if cls is None:
            continue
        out.append({"model": row["model"], "z_score": row["z_score"], **cls.as_dict()})
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.print_table(
        "Anomaly Classification",
        ["Model", "Z-Score", "Class", "Remediation", "Auto?", "Reason"],
        [
            [
                r["model"],
                f"{r['z_score']:.2f}",
                r["class"],
                r["remediation"],
                "yes" if r["autonomous_remediation_allowed"] else "no",
                r["reason"],
            ]
            for r in out
        ],
    )


@corruption_app.command("selftest", epilog=_EXAMPLES_SELFTEST)
def corruption_selftest(
    model: str = typer.Argument(..., help="Model whose recent predictions are the clean signal"),
    rate: float = typer.Option(0.20, "--rate", help="Fraction of values to corrupt per trial"),
    trials: int = typer.Option(20, "--trials", help="Injection trials per corruption class"),
):
    """Measure this detector against injected corruption and publish the rate (R-ef).

    A detector may not be credited with classes it was not tested against, so this injects
    each class into the model's own recent predictions and reports what was caught. A class
    the detector does not gate on is expected to score ~0 — printing that is the point.
    """
    from examlops.corruption import DETECTOR_COVERAGE, measure_detection_rate
    from examlops.data.drift import get_corruption_baseline

    init_db()
    baseline = get_corruption_baseline(model)
    if baseline is None:
        _output.error(
            f"No corruption baseline for {model}",
            hint=f"exa drift corruption baseline {model}",
        )
    with get_db() as conn:
        rows = conn.execute(
            "SELECT prediction FROM drift_snapshots WHERE model=? ORDER BY ts DESC, rowid DESC "
            "LIMIT ?",
            (model, _CORRUPTION_BASELINE_WINDOW),
        ).fetchall()
    clean = [r["prediction"] for r in rows]
    if not clean:
        _output.error(f"No prediction snapshots for {model}")
    measured = measure_detection_rate(clean, baseline, rate=rate, trials=trials)
    if _output.json_mode:
        _output.print_json({"model": model, "coverage": DETECTOR_COVERAGE, "measured": measured})
        return
    _output.print_table(
        f"Detection Rate — {model} (injected {rate:.0%}, {trials} trials/class)",
        ["Corruption Class", "Gating", "Detection Rate", "Detected By"],
        [
            [
                name,
                "yes" if measured[name]["gating"] else "no (reported only)",
                f"{measured[name]['detection_rate']:.0%}",
                str(DETECTOR_COVERAGE[name]["detected_by"]),
            ]
            for name in DETECTOR_COVERAGE
        ],
    )
    _output.detail(
        "False positive on the clean signal: "
        + ("YES" if measured["_clean"]["false_positive"] else "no")
    )


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
    """Input-drift rows. One implementation, in ``examlops.corruption``, because the
    ADR-0114 anomaly classifier consults exactly this statistic — a second copy here
    would be a second thing to keep in step with it."""
    from examlops.corruption import input_drift_rows

    return input_drift_rows(model_filter, window=_INPUT_WINDOW)


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
    reason: str | None = reason_option(),
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
    write_audit_event(
        "cli", actor, "input_baseline_set", model, audit_details({"n": len(snap_rows)}, reason)
    )
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
    reason: str | None = reason_option(),
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
    write_audit_event("cli", actor, "input_reset", model, audit_details({"cleared": n}, reason))
    _output.ok(f"Cleared {n} input snapshot(s) for {model}")


# ---------------------------------------------------------------------------
# C5 — advanced drift: concept / label-free perf / data-quality (ADR 0022)
# ---------------------------------------------------------------------------
_EXAMPLES_CONCEPT = (
    "Examples:\n\n"
    "  exa drift concept JPCP\n\n"
    "  exa drift concept JPCP --window 100\n\n"
    "  exa --json drift concept JPCP"
)


@app.command(epilog=_EXAMPLES_CONCEPT)
def concept(
    model: str = typer.Argument(..., help="Model name"),
    alias: str = typer.Option(None, "--alias", help="Restrict to one serving alias"),
    window: int = typer.Option(50, "--window", help="Recent window size (samples)"),
):
    """Concept-drift test on realized error as delayed labels arrive (C5·R1)."""
    from examlops.drift_advanced import detect_concept_drift

    init_db()
    res = detect_concept_drift(model, alias=alias, window=window)
    if _output.json_mode:
        _output.print_json(
            {
                "model": res.model,
                "drift_kind": res.drift_kind,
                "severity": res.severity,
                "score": res.score,
                "detail": res.detail,
            }
        )
        return
    color = {"OK": "green", "WARN": "yellow", "CRITICAL": "red"}.get(res.severity, "white")
    _output.info(f"Concept drift for {model}: [{color}]{res.severity}[/{color}]")
    if res.score is not None:
        _output.info(f"  z-score: {res.score:.2f}")
    if "recent_error" in res.detail:
        _output.info(
            f"  baseline error {res.detail['baseline_error']:.4f} → "
            f"recent {res.detail['recent_error']:.4f} (n={res.detail['n']})"
        )
    elif res.detail.get("reason"):
        _output.info(f"  {res.detail['reason']} (n={res.detail.get('n', 0)})")


@app.command()
def estimate(
    model: str = typer.Argument(..., help="Model name"),
    alias: str = typer.Option(None, "--alias", help="Restrict to one serving alias"),
    baseline: float = typer.Option(None, "--baseline", help="Baseline metric to compare against"),
    window: int = typer.Option(200, "--window", help="Recent predictions to estimate over"),
):
    """Label-free performance estimate (CBPE-like) before labels arrive (C5·R3/R4)."""
    from examlops.drift_advanced import estimate_performance

    init_db()
    res = estimate_performance(model, alias=alias, baseline=baseline, window=window)
    if _output.json_mode:
        _output.print_json(res)
        return
    if res["estimated"] is None:
        _output.info(f"No predictions recorded for {model} — nothing to estimate.")
        return
    _output.info(
        f"Estimated {res['metric']} for {model}: "
        f"{res['estimated']:.4f} ({res['method']}, n={res['n']})"
    )
    if res.get("realized") is not None:
        _output.info(f"  realized (labelled): {res['realized']:.4f}")
    if res.get("warn"):
        _output.warning(
            f"Estimated performance dropped ≥{int(0.10 * 100)}% vs baseline "
            f"{baseline:.4f} — warning only (awaiting labels)."
        )


@app.command()
def profile(
    model: str = typer.Argument(..., help="Model name"),
    last_n: int = typer.Option(200, "--last-n", help="Recent predictions to profile"),
    bad_payloads: int = typer.Option(0, "--bad-payloads", help="A5 bad-payload count to fold in"),
):
    """Profile recent inference inputs: schema / nulls / ranges / cardinality (C5·R5)."""
    import json as _json

    from examlops.drift_advanced import profile_inference

    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT features_json FROM predictions WHERE model=? ORDER BY id DESC LIMIT ?",
            (model, last_n),
        ).fetchall()
    batch = []
    for r in rows:
        if r["features_json"]:
            try:
                obj = _json.loads(r["features_json"])
                if isinstance(obj, dict):
                    batch.append(obj)
            except (ValueError, TypeError):
                continue
    prof = profile_inference(model, batch, bad_payloads=bad_payloads)
    if _output.json_mode:
        _output.print_json(
            {
                "model": prof.model,
                "n": prof.n,
                "null_fraction": prof.null_fraction,
                "severity": prof.severity,
                "fields": prof.fields,
            }
        )
        return
    color = {"OK": "green", "WARN": "yellow", "CRITICAL": "red"}.get(prof.severity, "white")
    _output.info(
        f"Data-quality profile for {model}: [{color}]{prof.severity}[/{color}] "
        f"(n={prof.n}, null_fraction={prof.null_fraction:.2%})"
    )
    if prof.fields:
        _output.print_table(
            "Fields",
            ["Field", "Nulls", "Null %", "Min", "Max", "Cardinality"],
            [
                [
                    k,
                    str(v["nulls"]),
                    f"{v['null_fraction']:.1%}",
                    str(v["min"]),
                    str(v["max"]),
                    str(v["cardinality"]),
                ]
                for k, v in prof.fields.items()
            ],
        )


@app.command()
def events(
    model: str = typer.Option(None, "--model", help="Filter to one model"),
    kind: str = typer.Option(
        None, "--kind", help="feature|prediction|input_embedding|concept|data_quality"
    ),
    last_n: int = typer.Option(30, "--last-n", help="Max events (newest first)"),
):
    """List unified drift events across all kinds (C5·R6)."""
    from examlops.data.drift import list_drift_events

    init_db()
    evs = list_drift_events(model=model, drift_kind=kind, last_n=last_n)
    if _output.json_mode:
        _output.print_json(evs)
        return
    if not evs:
        _output.info("No drift events recorded yet.")
        return
    _output.print_table(
        "Drift Events",
        ["Time", "Model", "Kind", "Severity", "Score"],
        [
            [
                e["ts"],
                e["model"],
                e["drift_kind"],
                e["severity"],
                f"{e['score']:.3f}" if e["score"] is not None else "—",
            ]
            for e in evs
        ],
    )


@app.command()
def forecast(
    model: str = typer.Argument(..., help="Model to forecast drift for"),
    threshold: float = typer.Option(3.0, "--threshold", help="Critical z-score threshold"),
    horizon: int = typer.Option(20, "--horizon", help="Look-ahead steps"),
):
    """Predict WHEN a model's drift will breach the threshold (pre-emptive, item 5.2).

    Fits a trend to recent prediction drift and projects the breach ETA, so the autopilot can
    retrain BEFORE the degradation window instead of after. Exit 1 if a breach is imminent.
    """
    from examlops.forecast import forecast_model_drift

    result = forecast_model_drift(model, threshold=threshold, horizon=horizon)
    if _output.json_mode:
        _output.print_json(result)
    elif result.get("reason"):
        _output.info(f"{model}: {result['reason']} — cannot forecast yet.")
    elif result["will_breach"]:
        eta = result["eta_steps"]
        when = "already breached" if eta == 0 else f"in ~{eta} step(s)"
        _output.warning(
            f"{model}: drift trend projects a breach of {threshold} {when} "
            f"(current z={result['current']}, slope={result['slope']})."
        )
    else:
        _output.ok(
            f"{model}: no breach forecast within {horizon} steps "
            f"(current z={result['current']}, slope={result['slope']})."
        )
    if result.get("will_breach"):
        raise typer.Exit(1)
