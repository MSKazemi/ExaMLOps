from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.platform_db import get_db, init_db, write_audit_event

app = typer.Typer(
    help="A/B testing experiments — compare two model variants.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_AB_TABLES = """
CREATE TABLE IF NOT EXISTS ab_tests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model      TEXT NOT NULL,
    name       TEXT,
    variant_a  TEXT NOT NULL DEFAULT 'Production',
    variant_b  TEXT NOT NULL DEFAULT 'Canary',
    split_pct  INTEGER NOT NULL DEFAULT 50,
    status     TEXT NOT NULL DEFAULT 'running',
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at   DATETIME,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS ab_results (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL,
    ts      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    variant TEXT NOT NULL,
    value   REAL NOT NULL
);
"""

_EXAMPLES_START = (
    "Examples:\n\n"
    "  exa serve ab start JPCP\n\n"
    "  exa serve ab start JPCP --variant-a Production --variant-b Canary --split 70\n\n"
    "  exa serve ab start JPCP --name exp-001 --split 80"
)
_EXAMPLES_STOP = "Examples:\n\n  exa serve ab stop JPCP"
_EXAMPLES_STATUS = (
    "Examples:\n\n"
    "  exa serve ab status\n\n"
    "  exa serve ab status JPCP\n\n"
    "  exa --json serve ab status"
)
_EXAMPLES_RECORD = (
    "Examples:\n\n"
    "  exa serve ab record JPCP Production 0.92\n\n"
    "  exa serve ab record JPCP Canary 0.87"
)


def _ensure_ab_tables() -> None:
    init_db()
    with get_db() as conn:
        conn.executescript(_AB_TABLES)


@app.command("start", epilog=_EXAMPLES_START)
def ab_start(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    variant_a: str = typer.Option("Production", "--variant-a", "-a", help="First variant alias"),
    variant_b: str = typer.Option("Canary", "--variant-b", "-b", help="Second variant alias"),
    split: int = typer.Option(
        50, "--split", "-s", help="% of traffic routed to variant_a (rest goes to variant_b)"
    ),
    name: str | None = typer.Option(None, "--name", "-n", help="Optional experiment name"),
) -> None:
    """Start a new A/B test comparing two model variants."""
    _ensure_ab_tables()

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM ab_tests WHERE model=? AND status='running'",
            (model,),
        ).fetchone()
        if existing:
            _output.error(
                f"An active A/B test already exists for {model} (id={existing['id']}). "
                "Stop it first with: exa serve ab stop " + model
            )  # _output.error raises typer.Exit(1)

        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
        conn.execute(
            "INSERT INTO ab_tests (model, name, variant_a, variant_b, split_pct, status, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (model, name, variant_a, variant_b, split, actor),
        )

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    write_audit_event(
        "cli",
        actor,
        "ab_test_started",
        model,
        {"variant_a": variant_a, "variant_b": variant_b, "split_pct": split, "name": name},
    )
    _output.ok(f"A/B test started for {model}: {variant_a} vs {variant_b} ({split}/{100 - split}%)")


@app.command("stop", epilog=_EXAMPLES_STOP)
def ab_stop(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
) -> None:
    """Stop the running A/B test for a model."""
    _ensure_ab_tables()

    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE ab_tests SET status='completed', ended_at=CURRENT_TIMESTAMP "
            "WHERE model=? AND status='running'",
            (model,),
        )
        affected = cursor.rowcount

    if affected == 0:
        _output.warning(f"No running A/B test found for {model}.")
        return

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    write_audit_event("cli", actor, "ab_test_stopped", model, {})
    _output.ok(f"A/B test for {model} marked as completed.")


@app.command("status", epilog=_EXAMPLES_STATUS)
def ab_status(
    model: str | None = typer.Argument(None, help="Filter by model name (default: all)"),
) -> None:
    """Show A/B tests (most recent 20)."""
    _ensure_ab_tables()

    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT model, name, variant_a, variant_b, split_pct, status, started_at "
                "FROM ab_tests WHERE model=? ORDER BY started_at DESC LIMIT 20",
                (model,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT model, name, variant_a, variant_b, split_pct, status, started_at "
                "FROM ab_tests ORDER BY started_at DESC LIMIT 20",
            ).fetchall()

    if not rows:
        label = f"for {model}" if model else ""
        _output.ok(f"No A/B tests found {label}".strip() + ".")
        return

    if _output.json_mode:
        _output.print_json([dict(r) for r in rows])
        return

    table_rows = [
        [
            r["model"],
            r["name"] or "—",
            r["variant_a"],
            r["variant_b"],
            f"{r['split_pct']}/{100 - r['split_pct']}%",
            r["status"],
            (r["started_at"] or "—")[:19],
        ]
        for r in rows
    ]
    _output.print_table(
        "A/B Tests",
        ["Model", "Name", "Variant A", "Variant B", "Split", "Status", "Started"],
        table_rows,
    )


@app.command("record", epilog=_EXAMPLES_RECORD)
def ab_record(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    variant: str = typer.Argument(..., help="Variant alias (e.g. Production or Canary)"),
    value: float = typer.Argument(..., help="Metric value to record"),
) -> None:
    """Record a metric observation for the active A/B test of a model."""
    _ensure_ab_tables()

    with get_db() as conn:
        test_row = conn.execute(
            "SELECT id FROM ab_tests WHERE model=? AND status='running' "
            "ORDER BY started_at DESC LIMIT 1",
            (model,),
        ).fetchone()
        if test_row is None:
            _output.error(
                f"No running A/B test found for {model}. "
                "Start one with: exa serve ab start " + model
            )  # _output.error raises typer.Exit(1)

        test_id = test_row["id"]
        conn.execute(
            "INSERT INTO ab_results (test_id, variant, value) VALUES (?, ?, ?)",
            (test_id, variant, value),
        )

    _output.ok(f"Recorded {value} for variant '{variant}' in A/B test #{test_id} ({model}).")
