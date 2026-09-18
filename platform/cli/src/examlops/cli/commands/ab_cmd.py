from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.data import get_db, init_db
from examlops.data.audit import write_audit_event

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
_EXAMPLES_ANALYZE = (
    "Examples:\n\n"
    "  exa serve ab analyze JPCP\n\n"
    "  exa serve ab analyze JPCP --lower-is-better   # e.g. RMSE / latency metrics\n\n"
    "  exa serve ab analyze JPCP --alpha 0.01 --min-sample 100"
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
            "ORDER BY started_at DESC, id DESC LIMIT 1",
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


@app.command("analyze", epilog=_EXAMPLES_ANALYZE)
def ab_analyze(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    lower_is_better: bool = typer.Option(
        False,
        "--lower-is-better",
        help="Metric where smaller wins (e.g. RMSE, latency); default higher-is-better",
    ),
    alpha: float = typer.Option(0.05, "--alpha", help="Significance level"),
    min_sample: int = typer.Option(
        30, "--min-sample", help="Minimum observations per variant before calling a winner"
    ),
) -> None:
    """Run a statistical test on the recorded observations of a model's active A/B test.

    Uses Welch's t-test (unequal variance) and reports whether the difference between the
    two variants is significant, plus the winner given the metric's optimisation direction.
    """
    try:
        from examlops.analysis.ab_stats import analyze_ab
    except ModuleNotFoundError as exc:  # numpy/scipy are the optional `analysis` extra
        _output.error(
            f"A/B analysis needs the optional scientific stack ({exc.name}). "
            "Install it with: uv pip install 'examlops[analysis]'"
        )

    _ensure_ab_tables()
    with get_db() as conn:
        test_row = conn.execute(
            "SELECT id, variant_a, variant_b FROM ab_tests WHERE model=? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (model,),
        ).fetchone()
        if test_row is None:
            _output.error(
                f"No A/B test found for {model}. Start one with: exa serve ab start {model}"
            )
        test_id = test_row["id"]
        variant_a, variant_b = test_row["variant_a"], test_row["variant_b"]
        rows = conn.execute(
            "SELECT variant, value FROM ab_results WHERE test_id=?", (test_id,)
        ).fetchall()

    values_a = [r["value"] for r in rows if r["variant"] == variant_a]
    values_b = [r["value"] for r in rows if r["variant"] == variant_b]

    result = analyze_ab(
        values_a,
        values_b,
        lower_is_better=lower_is_better,
        alpha=alpha,
        min_sample=min_sample,
    )

    if _output.json_mode:
        _output.print_json({"model": model, "test_id": test_id, **result})
        return

    if result["verdict"] == "insufficient_sample":
        _output.warning(
            f"Insufficient sample for {model}: {variant_a} n={result['n_a']}, "
            f"{variant_b} n={result['n_b']} (need ≥ {result['min_sample']} each)."
        )
        return

    winner_label = "—"
    if result["winner"] == "a":
        winner_label = variant_a
    elif result["winner"] == "b":
        winner_label = variant_b

    _output.print_table(
        f"A/B analysis — {model} (#{test_id})",
        ["Field", "Value"],
        [
            [variant_a, f"mean={result['mean_a']:.4g}  n={result['n_a']}"],
            [variant_b, f"mean={result['mean_b']:.4g}  n={result['n_b']}"],
            ["t-stat", f"{result['t_stat']:.4f}"],
            ["p-value", f"{result['p_value']:.4g}"],
            ["significant", "yes" if result["significant"] else "no"],
            ["winner", winner_label],
        ],
    )
    write_audit_event(
        "cli",
        os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli",
        "ab_test_analyzed",
        model,
        {
            "p_value": result["p_value"],
            "winner": winner_label,
            "significant": result["significant"],
        },
    )
