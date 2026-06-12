from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.platform_db import get_db, init_db, write_audit_event

app = typer.Typer(
    help="Shadow deployment traffic mirroring.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_ENABLE = (
    "Examples:\n\n"
    "  exa serve shadow enable JPCP\n\n"
    "  exa serve shadow enable JPCP --shadow-alias Canary"
)
_EXAMPLES_DISABLE = "Examples:\n\n  exa serve shadow disable JPCP"
_EXAMPLES_STATUS = (
    "Examples:\n\n"
    "  exa serve shadow status\n\n"
    "  exa serve shadow status JPCP"
)
_EXAMPLES_LOG = "Examples:\n\n  exa serve shadow log JPCP"


def _ensure_tables() -> None:
    init_db()
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS shadow_config (
                model        TEXT PRIMARY KEY,
                shadow_alias TEXT NOT NULL DEFAULT 'Staging',
                enabled      INTEGER NOT NULL DEFAULT 1,
                updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by   TEXT
            );
            CREATE TABLE IF NOT EXISTS shadow_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model           TEXT NOT NULL,
                production_pred REAL,
                shadow_pred     REAL,
                diff_pct        REAL,
                job_id          TEXT
            );
        """)


@app.command("enable", epilog=_EXAMPLES_ENABLE)
def shadow_enable(
    model: str = typer.Argument(..., help="Model name (uppercase, e.g. JPCP)"),
    shadow_alias: str = typer.Option("Staging", "--shadow-alias", "-a", help="MLflow alias to mirror traffic to"),
) -> None:
    """Enable shadow deployment for a model."""
    _ensure_tables()
    actor = os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO shadow_config
               (model, shadow_alias, enabled, updated_at, updated_by)
               VALUES (?, ?, 1, CURRENT_TIMESTAMP, ?)""",
            (model, shadow_alias, actor),
        )
    write_audit_event(
        source="cli",
        actor=actor,
        action="shadow_enable",
        target=model,
        details={"shadow_alias": shadow_alias},
    )
    _output.ok(f"Shadow deployment enabled for [bold]{model}[/bold] → alias [cyan]{shadow_alias}[/cyan]")


@app.command("disable", epilog=_EXAMPLES_DISABLE)
def shadow_disable(
    model: str = typer.Argument(..., help="Model name (uppercase, e.g. JPCP)"),
) -> None:
    """Disable shadow deployment for a model."""
    _ensure_tables()
    actor = os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))
    with get_db() as conn:
        conn.execute(
            "UPDATE shadow_config SET enabled=0, updated_at=CURRENT_TIMESTAMP, updated_by=? WHERE model=?",
            (actor, model),
        )
    write_audit_event(
        source="cli",
        actor=actor,
        action="shadow_disable",
        target=model,
        details=None,
    )
    _output.ok(f"Shadow deployment disabled for [bold]{model}[/bold]")


@app.command("status", epilog=_EXAMPLES_STATUS)
def shadow_status(
    model: str | None = typer.Argument(None, help="Filter by model name (optional)"),
) -> None:
    """Show shadow deployment configuration."""
    _ensure_tables()
    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT model, shadow_alias, enabled, updated_at FROM shadow_config WHERE model=?",
                (model,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT model, shadow_alias, enabled, updated_at FROM shadow_config ORDER BY model",
            ).fetchall()

    if not rows:
        _output.info("No shadow deployment configuration found.")
        return

    table_rows = [
        [r["model"], r["shadow_alias"], "yes" if r["enabled"] else "no", r["updated_at"]]
        for r in rows
    ]
    _output.print_table(
        "Shadow Deployment Configuration",
        ["Model", "Shadow Alias", "Enabled", "Updated"],
        table_rows,
    )


@app.command("log", epilog=_EXAMPLES_LOG)
def shadow_log(
    model: str = typer.Argument(..., help="Model name (uppercase, e.g. JPCP)"),
) -> None:
    """Show last 20 shadow inference comparison results for a model."""
    _ensure_tables()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ts, model, production_pred, shadow_pred, diff_pct
               FROM shadow_results
               WHERE model=?
               ORDER BY ts DESC, id DESC
               LIMIT 20""",
            (model,),
        ).fetchall()

    if not rows:
        _output.info(f"No shadow results found for model [bold]{model}[/bold].")
        return

    table_rows = [
        [
            r["ts"],
            r["model"],
            r["production_pred"],
            r["shadow_pred"],
            f"{r['diff_pct']:.2f}%" if r["diff_pct"] is not None else "—",
        ]
        for r in rows
    ]
    _output.print_table(
        f"Shadow Results — {model}",
        ["Time", "Model", "Prod Pred", "Shadow Pred", "Diff%"],
        table_rows,
    )
