from __future__ import annotations

import os

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.platform_db import get_db, init_db, write_audit_event

app = typer.Typer(
    help="Hyperparameter optimisation",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_START = (
    "Examples:\n\n"
    "  exa pipeline hpo start JPCP\n\n"
    "  exa pipeline hpo start JPCP --trials 50 --metric rmse --dataset PM100Dataset"
)
_EXAMPLES_STATUS = (
    "Examples:\n\n"
    "  exa pipeline hpo status\n\n"
    "  exa pipeline hpo status JPCP"
)
_EXAMPLES_RECORD = (
    "Examples:\n\n"
    "  exa pipeline hpo record JPCP --trial 1 --params '{\"n_estimators\": 100}' --value 10.5"
)


def _ensure_hpo_tables() -> None:
    init_db()
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hpo_studies (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model            TEXT NOT NULL,
                dataset          TEXT,
                n_trials         INTEGER NOT NULL DEFAULT 20,
                metric           TEXT NOT NULL DEFAULT 'rmse',
                status           TEXT NOT NULL DEFAULT 'pending',
                flow_run_id      TEXT,
                best_params_json TEXT,
                best_value       REAL,
                actor            TEXT
            );
            CREATE TABLE IF NOT EXISTS hpo_trials (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                study_id    INTEGER NOT NULL,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                trial_num   INTEGER NOT NULL,
                params_json TEXT NOT NULL,
                value       REAL
            );
        """)


@app.command("start", epilog=_EXAMPLES_START)
def hpo_start(
    model: str = typer.Argument(..., help="Model ID (e.g. JPCP)"),
    trials: int = typer.Option(20, "--trials", "-t", help="Number of HPO trials"),
    metric: str = typer.Option("rmse", "--metric", "-m", help="Metric to optimise"),
    dataset: str = typer.Option("PM100Dataset", "--dataset", "-d", help="Dataset class name"),
) -> None:
    """Trigger an HPO study via the Control Plane."""
    cfg = load_config()
    _ensure_hpo_tables()

    body = {
        "model": model,
        "dataset": dataset,
        "hpo_trials": trials,
    }

    with _output.spinner(f"Starting HPO study for {model} ({trials} trials)..."):
        try:
            result = _client.post(
                f"{cfg.control_plane_url}/retrain",
                body,
                token=cfg.control_plane_token,
            )
        except _client.ClientError as e:
            _output.error(
                f"Failed to start HPO study for {model}: {e}",
                hint="Is the control plane running? Try: exa status",
            )
            return

    flow_run_id = result.get("flow_run_id", "")
    actor = os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))

    with get_db() as conn:
        conn.execute(
            "INSERT INTO hpo_studies (model, dataset, n_trials, metric, status, flow_run_id, actor)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (model, dataset, trials, metric, "pending", flow_run_id, actor),
        )

    write_audit_event(
        source="cli:hpo",
        actor=actor,
        action="hpo_start",
        target=model,
        details={"trials": trials, "metric": metric, "dataset": dataset, "flow_run_id": flow_run_id},
    )

    _output.ok(
        f"HPO study started (flow_run_id={flow_run_id}, {trials} trials)"
    )
    _output.print_record({
        "model":       model,
        "dataset":     dataset,
        "trials":      trials,
        "metric":      metric,
        "flow_run_id": flow_run_id or "—",
        "status":      "pending",
    })


@app.command("status", epilog=_EXAMPLES_STATUS)
def hpo_status(
    model: str | None = typer.Argument(None, help="Filter by model ID"),
) -> None:
    """Show HPO study status."""
    _ensure_hpo_tables()

    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT ts, model, n_trials, metric, status, best_value, flow_run_id"
                " FROM hpo_studies WHERE model=? ORDER BY ts DESC LIMIT 20",
                (model,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, model, n_trials, metric, status, best_value, flow_run_id"
                " FROM hpo_studies ORDER BY ts DESC LIMIT 20"
            ).fetchall()

    if not rows:
        _output.info("No HPO studies found.")
        return

    _output.print_table(
        "HPO Studies",
        ["Time", "Model", "Trials", "Metric", "Status", "Best Value", "Flow Run"],
        [
            [
                r["ts"],
                r["model"],
                r["n_trials"],
                r["metric"],
                r["status"],
                r["best_value"] if r["best_value"] is not None else "—",
                r["flow_run_id"] or "—",
            ]
            for r in rows
        ],
    )


@app.command("record", epilog=_EXAMPLES_RECORD)
def hpo_record(
    model: str = typer.Argument(..., help="Model ID (e.g. JPCP)"),
    trial: int = typer.Option(..., "--trial", help="Trial number"),
    params_json: str = typer.Option(..., "--params", help="Trial parameters as JSON string"),
    value: float = typer.Option(..., "--value", help="Metric value for this trial"),
) -> None:
    """Record an HPO trial result."""
    _ensure_hpo_tables()

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM hpo_studies WHERE model=? ORDER BY ts DESC LIMIT 1",
            (model,),
        ).fetchone()

    if not row:
        _output.error(
            f"No HPO study found for model '{model}'.",
            hint=f"Start one first: exa pipeline hpo start {model}",
        )
        return

    study_id = row["id"]

    with get_db() as conn:
        conn.execute(
            "INSERT INTO hpo_trials (study_id, trial_num, params_json, value)"
            " VALUES (?, ?, ?, ?)",
            (study_id, trial, params_json, value),
        )

    _output.ok(f"Trial {trial} recorded (study_id={study_id}, value={value})")
