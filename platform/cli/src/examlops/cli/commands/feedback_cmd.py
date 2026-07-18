from __future__ import annotations

import csv
import math
import os
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.data import init_db
from examlops.data.audit import write_audit_event
from examlops.data.finops import (
    get_live_metrics,
    join_predictions_with_truth,
    write_ground_truth,
    write_live_metric,
)

app = typer.Typer(
    help="Ground-truth feedback loop — join delayed labels to predictions and measure live accuracy.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_INGEST = (
    "Examples:\n\n"
    "  exa eval feedback ingest --request-hash hash-1 --label 88.5\n\n"
    "  exa eval feedback ingest --from-csv labels.csv   # columns: request_hash,label[,source]"
)
_EXAMPLES_JOIN = (
    "Examples:\n\n  exa eval feedback join JPCP\n\n  exa eval feedback join JPCP --alias Production"
)
_EXAMPLES_ACCURACY = (
    "Examples:\n\n"
    "  exa eval feedback accuracy JPCP\n\n"
    "  exa eval feedback accuracy JPCP --alias Production --record"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))


@app.command("ingest", epilog=_EXAMPLES_INGEST)
def feedback_ingest(
    request_hash: str | None = typer.Option(
        None, "--request-hash", "-r", help="Join key of the prediction to label"
    ),
    label: float | None = typer.Option(None, "--label", "-l", help="Observed ground-truth value"),
    source: str = typer.Option("manual", "--source", "-s", help="Provenance of the label"),
    from_csv: Path | None = typer.Option(
        None, "--from-csv", help="Bulk-ingest a CSV with columns request_hash,label[,source]"
    ),
) -> None:
    """Ingest delayed ground-truth label(s), keyed by prediction request_hash."""
    init_db()
    n = 0
    if from_csv is not None:
        if not from_csv.exists():
            _output.error(f"CSV not found: {from_csv}")
            raise typer.Exit(1)
        with from_csv.open(newline="") as fh:
            for row in csv.DictReader(fh):
                if not row.get("request_hash") or row.get("label") in (None, ""):
                    continue
                write_ground_truth(
                    row["request_hash"], float(row["label"]), row.get("source") or "csv"
                )
                n += 1
    else:
        if request_hash is None or label is None:
            _output.error("Provide --request-hash and --label, or --from-csv.")
            raise typer.Exit(1)
        write_ground_truth(request_hash, label, source)
        n = 1

    write_audit_event(
        source="cli",
        actor=_actor(),
        action="feedback_ingest",
        target=None,
        details={"n": n, "from_csv": str(from_csv) if from_csv else None},
    )
    _output.ok(f"Ingested [bold]{n}[/bold] ground-truth label(s).")


@app.command("join", epilog=_EXAMPLES_JOIN)
def feedback_join(
    model: str = typer.Argument(..., help="Model name (uppercase, e.g. JPCP)"),
    alias: str | None = typer.Option(None, "--alias", "-a", help="Filter by MLflow alias"),
) -> None:
    """Show prediction/label pairs joined on request_hash (delayed-label join)."""
    init_db()
    rows = join_predictions_with_truth(model, alias=alias)
    if not rows:
        _output.info(f"No labelled predictions yet for [bold]{model}[/bold].")
        return
    table_rows = [
        [r["request_hash"], r["alias"], f"{r['prediction']:.4g}", f"{r['label']:.4g}", r["source"]]
        for r in rows
    ]
    _output.print_table(
        f"Labelled predictions — {model}",
        ["Request", "Alias", "Prediction", "Label", "Source"],
        table_rows,
    )


def _accuracy_metrics(pairs: list[dict]) -> dict[str, float]:
    """Compute RMSE / MAE over joined (prediction, label) pairs."""
    n = len(pairs)
    if n == 0:
        return {}
    se = sum((p["prediction"] - p["label"]) ** 2 for p in pairs)
    ae = sum(abs(p["prediction"] - p["label"]) for p in pairs)
    return {"rmse": math.sqrt(se / n), "mae": ae / n, "n": float(n)}


@app.command("accuracy", epilog=_EXAMPLES_ACCURACY)
def feedback_accuracy(
    model: str = typer.Argument(..., help="Model name (uppercase, e.g. JPCP)"),
    alias: str | None = typer.Option(None, "--alias", "-a", help="Restrict to one MLflow alias"),
    record: bool = typer.Option(
        False, "--record", help="Persist computed metrics to the live_metrics table"
    ),
) -> None:
    """Compute live accuracy (RMSE/MAE) from labelled predictions — real quality, not drift proxy."""
    init_db()
    pairs = join_predictions_with_truth(model, alias=alias)
    metrics = _accuracy_metrics(pairs)
    if not metrics:
        _output.info(
            f"No labelled predictions for [bold]{model}[/bold]"
            + (f" (alias {alias})" if alias else "")
            + " — ingest ground truth first."
        )
        return

    effective_alias = alias or "all"
    if record:
        for name in ("rmse", "mae"):
            write_live_metric(model, effective_alias, name, metrics[name], n=int(metrics["n"]))
        write_audit_event(
            source="cli",
            actor=_actor(),
            action="feedback_accuracy_recorded",
            target=model,
            details={"alias": effective_alias, **metrics},
        )

    _output.print_table(
        f"Live accuracy — {model} ({effective_alias})",
        ["Metric", "Value", "N"],
        [
            ["rmse", f"{metrics['rmse']:.4f}", int(metrics["n"])],
            ["mae", f"{metrics['mae']:.4f}", int(metrics["n"])],
        ],
    )
    if record:
        _output.ok(
            f"Recorded {len(get_live_metrics(model, alias=effective_alias))} live-metric row(s)."
        )
