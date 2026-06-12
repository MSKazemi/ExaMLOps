from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.cli._config import load_config
from examlops.platform_db import get_db, init_db, write_audit_event

_CREATE_BATCH_JOBS = """
CREATE TABLE IF NOT EXISTS batch_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model       TEXT NOT NULL,
    alias       TEXT NOT NULL DEFAULT 'Production',
    input_path  TEXT,
    output_path TEXT,
    n_inputs    INTEGER,
    n_success   INTEGER,
    n_errors    INTEGER,
    elapsed_s   REAL,
    actor       TEXT
);
"""


def _ensure_table() -> None:
    """Create batch_jobs table if it does not exist yet."""
    init_db()
    with get_db() as conn:
        conn.executescript(_CREATE_BATCH_JOBS)

app = typer.Typer(
    help="Batch inference jobs",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_MAX_ROWS = 1000

_EXAMPLES_SUBMIT = (
    "Examples:\n\n"
    "  # Submit a JSON array file\n"
    "  exa serve batch submit JPCP inputs.json\n\n"
    "  # Save predictions to a file\n"
    "  exa serve batch submit JPCP inputs.json --output predictions.json\n\n"
    "  # Use the Canary alias\n"
    "  exa serve batch submit JPCP inputs.json --alias Canary"
)

_EXAMPLES_LIST = (
    "Examples:\n\n"
    "  # List all recent batch jobs\n"
    "  exa serve batch list\n\n"
    "  # Filter by model\n"
    "  exa serve batch list --model JPCP"
)


def _load_inputs(input_file: Path) -> list[dict]:
    """Load input records from a JSON array file or JSONL file."""
    text = input_file.read_text()
    stripped = text.strip()
    if stripped.startswith("["):
        rows = json.loads(stripped)
        if not isinstance(rows, list):
            raise ValueError("JSON file must contain a top-level array")
        return rows
    # JSONL — one JSON object per line
    rows = []
    for line in stripped.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


@app.command("submit", epilog=_EXAMPLES_SUBMIT)
def batch_submit(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    input_file: Path = typer.Argument(..., help="JSON array or JSONL file of input dicts"),
    alias: str = typer.Option("Production", "--alias", "-a", help="MLflow alias to target"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write predictions JSON here"),
) -> None:
    """Run synchronous batch inference from a JSON/JSONL input file."""
    if not input_file.exists():
        _output.error(f"Input file not found: {input_file}")
        raise typer.Exit(1)

    try:
        rows = _load_inputs(input_file)
    except (json.JSONDecodeError, ValueError) as exc:
        _output.error(f"Failed to parse input file: {exc}")
        raise typer.Exit(1)

    if len(rows) > _MAX_ROWS:
        _output.error(f"Input file has {len(rows)} rows — max is {_MAX_ROWS}")
        raise typer.Exit(1)

    n_total = len(rows)
    if n_total == 0:
        _output.error("Input file is empty")
        raise typer.Exit(1)

    cfg = load_config()
    predict_url = f"{cfg.ray_serve_url}/predict/{model}"

    results: list[dict] = []
    n_success = 0
    n_errors = 0
    t_start = time.monotonic()

    with _output.spinner(f"Running batch inference for {model} ({n_total} inputs)…"):
        for i, row in enumerate(rows):
            body = json.dumps(row).encode()
            req = urllib.request.Request(
                predict_url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read())
                prediction = resp_data.get("prediction", resp_data)
                results.append({"index": i, "prediction": prediction, "error": None})
                n_success += 1
            except urllib.error.URLError as exc:
                results.append({"index": i, "prediction": None, "error": str(exc)})
                n_errors += 1

    elapsed = time.monotonic() - t_start

    # Write output file if requested
    if output is not None:
        output.write_text(json.dumps(results, indent=2))

    # Record job in DB
    _ensure_table()
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO batch_jobs
                (model, alias, input_path, output_path, n_inputs, n_success, n_errors, elapsed_s, actor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model,
                alias,
                str(input_file),
                str(output) if output else None,
                n_total,
                n_success,
                n_errors,
                round(elapsed, 3),
                actor,
            ),
        )

    write_audit_event(
        "cli",
        actor,
        "batch_submit",
        model,
        {"alias": alias, "n_inputs": n_total, "n_success": n_success, "n_errors": n_errors},
    )

    _output.ok(f"Batch complete: {n_success}/{n_total} succeeded in {elapsed:.1f}s")
    if n_errors:
        _output.warning(f"{n_errors} request(s) failed — check output for details")
    if output:
        _output.ok(f"Predictions written to {output}")

    if _output.json_mode:
        _output.print_json(results)


@app.command("list", epilog=_EXAMPLES_LIST)
def batch_list(
    model: str | None = typer.Option(None, "--model", "-m", help="Filter by model name"),
) -> None:
    """List recent batch inference jobs."""
    _ensure_table()
    with get_db() as conn:
        if model:
            rows = conn.execute(
                """
                SELECT ts, model, alias, n_inputs, n_success, n_errors, elapsed_s, input_path
                FROM batch_jobs
                WHERE model = ?
                ORDER BY ts DESC
                LIMIT 20
                """,
                (model,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT ts, model, alias, n_inputs, n_success, n_errors, elapsed_s, input_path
                FROM batch_jobs
                ORDER BY ts DESC
                LIMIT 20
                """
            ).fetchall()

    if not rows:
        _output.ok("No batch jobs recorded")
        return

    if _output.json_mode:
        _output.print_json([dict(r) for r in rows])
        return

    table_rows = [
        [
            (r["ts"] or "—")[:19],
            r["model"] or "—",
            str(r["n_inputs"] or 0),
            str(r["n_success"] or 0),
            str(r["n_errors"] or 0),
            f"{r['elapsed_s']:.1f}s" if r["elapsed_s"] is not None else "—",
            r["input_path"] or "—",
        ]
        for r in rows
    ]
    _output.print_table(
        "Batch Jobs",
        ["Time", "Model", "N In", "N OK", "N Err", "Elapsed", "Input"],
        table_rows,
    )
