from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.platform_db import get_db, init_db, write_audit_event

app = typer.Typer(
    help="Data quality validation gates",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_CHECK = (
    "Examples:\n\n"
    "  exa pipeline quality check JPCP PM100Dataset\n\n"
    "  exa --json pipeline quality check JPCP PM100Dataset"
)
_EXAMPLES_HISTORY = (
    "Examples:\n\n"
    "  exa pipeline quality history JPCP\n\n"
    "  exa --json pipeline quality history JPCP"
)


def _init_quality_table() -> None:
    """Ensure data_quality_checks table exists (called in addition to init_db)."""
    init_db()
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS data_quality_checks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model        TEXT NOT NULL,
                dataset      TEXT NOT NULL,
                status       TEXT NOT NULL,
                passed       INTEGER NOT NULL DEFAULT 0,
                failed       INTEGER NOT NULL DEFAULT 0,
                details_json TEXT,
                actor        TEXT
            )
        """)


def _data_cache_root() -> Path:
    """Return the .data_cache root relative to cwd or the PLATFORM_DB directory."""
    db_env = os.getenv("PLATFORM_DB")
    if db_env:
        base = Path(db_env).parent
    else:
        base = Path.cwd()
    return base / ".data_cache"


@app.command("check", epilog=_EXAMPLES_CHECK)
def quality_check(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    dataset: str = typer.Argument(..., help="Dataset name (e.g. PM100Dataset)"),
) -> None:
    """Run data quality checks for a model/dataset pair and record results."""
    _init_quality_table()
    actor = os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))

    data_dir = _data_cache_root() / dataset

    checks: list[tuple[str, bool, str]] = []  # (check_name, passed, detail)

    # Check (a): directory exists
    dir_exists = data_dir.exists() and data_dir.is_dir()
    checks.append((
        "dir_exists",
        dir_exists,
        str(data_dir) if dir_exists else f"Directory not found: {data_dir}",
    ))

    # Checks (b) and (c): files present — only meaningful if dir exists
    if dir_exists:
        data_files = [
            f for f in data_dir.iterdir()
            if f.is_file() and f.suffix in {".parquet", ".json"}
        ]
        has_files = len(data_files) > 0
        checks.append((
            "data_files_present",
            has_files,
            f"{len(data_files)} file(s) found" if has_files else "No .parquet or .json files found",
        ))
        checks.append((
            "file_count_gt_zero",
            has_files,
            f"file count = {len(data_files)}",
        ))
    else:
        checks.append(("data_files_present", False, "Skipped — directory missing"))
        checks.append(("file_count_gt_zero", False, "Skipped — directory missing"))

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = sum(1 for _, ok, _ in checks if not ok)

    # Determine overall status
    if not dir_exists:
        overall = "warn"
    elif failed > 0:
        overall = "fail"
    else:
        overall = "pass"

    details = [
        {"check": name, "status": "pass" if ok else "fail", "detail": detail}
        for name, ok, detail in checks
    ]
    details_json = json.dumps(details)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO data_quality_checks
               (model, dataset, status, passed, failed, details_json, actor)
               VALUES (?,?,?,?,?,?,?)""",
            (model, dataset, overall, passed, failed, details_json, actor),
        )

    write_audit_event(
        "cli", actor, "quality_check",
        model, {"dataset": dataset, "status": overall, "passed": passed, "failed": failed},
    )

    rows = [
        [d["check"], d["status"].upper(), d["detail"]]
        for d in details
    ]
    _output.print_table(
        f"Data Quality: {model} / {dataset}",
        ["Check", "Status", "Detail"],
        rows,
    )

    status_colour = {"pass": "green", "warn": "yellow", "fail": "red"}[overall]
    if not _output.json_mode:
        _output.console.print(
            f"\nOverall: [{status_colour}]{overall.upper()}[/{status_colour}]  "
            f"passed={passed}  failed={failed}"
        )


@app.command("history", epilog=_EXAMPLES_HISTORY)
def quality_history(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
) -> None:
    """Show data quality check history for a model (last 20 runs)."""
    _init_quality_table()

    with get_db() as conn:
        rows = conn.execute(
            """SELECT ts, dataset, status, passed, failed
               FROM data_quality_checks
               WHERE model=?
               ORDER BY ts DESC
               LIMIT 20""",
            (model,),
        ).fetchall()

    if not rows:
        _output.info(f"No quality checks recorded for {model}.")
        return

    table_rows = [
        [r["ts"], r["dataset"], r["status"].upper(), str(r["passed"]), str(r["failed"])]
        for r in rows
    ]
    _output.print_table(
        f"Quality History: {model}",
        ["Time", "Dataset", "Status", "Passed", "Failed"],
        table_rows,
    )
