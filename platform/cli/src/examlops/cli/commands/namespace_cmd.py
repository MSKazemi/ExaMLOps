from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.data import get_db, init_db
from examlops.data.audit import write_audit_event

app = typer.Typer(
    help="Project namespace isolation.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _ensure_namespace_tables() -> None:
    """Create namespace tables if they do not yet exist."""
    init_db()
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS namespaces (
                name        TEXT PRIMARY KEY,
                description TEXT,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by  TEXT
            );
            CREATE TABLE IF NOT EXISTS namespace_models (
                model       TEXT NOT NULL,
                namespace   TEXT NOT NULL DEFAULT 'default',
                assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (model, namespace)
            );
        """)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command("list")
def ns_list() -> None:
    """List all namespaces with model counts."""
    _ensure_namespace_tables()
    with get_db() as conn:
        # Ensure the default namespace always exists
        conn.execute("INSERT OR IGNORE INTO namespaces (name) VALUES ('default')")
        rows = conn.execute(
            "SELECT name, description, created_at FROM namespaces ORDER BY name"
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            result = conn.execute(
                "SELECT COUNT(*) AS cnt FROM namespace_models WHERE namespace=?",
                (row["name"],),
            ).fetchone()
            counts[row["name"]] = result["cnt"] if result else 0

    if _output.json_mode:
        _output.print_json(
            [
                {
                    "name": r["name"],
                    "description": r["description"],
                    "models": counts[r["name"]],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        )
        return

    if not rows:
        _output.warning("No namespaces found.")
        return

    _output.print_table(
        "Namespaces",
        ["Name", "Description", "Models", "Created"],
        [
            [
                r["name"],
                r["description"] or "",
                str(counts[r["name"]]),
                r["created_at"],
            ]
            for r in rows
        ],
    )


@app.command("create")
def ns_create(
    name: str = typer.Argument(..., help="Namespace name (unique identifier)"),
    description: str | None = typer.Option(
        None, "--description", "-d", help="Optional description"
    ),
) -> None:
    """Create a new namespace."""
    _ensure_namespace_tables()
    with get_db() as conn:
        existing = conn.execute("SELECT name FROM namespaces WHERE name=?", (name,)).fetchone()
        if existing:
            _output.error(f"Namespace '{name}' already exists")
            raise typer.Exit(1)
        actor = _actor()
        conn.execute(
            "INSERT INTO namespaces (name, description, created_by) VALUES (?,?,?)",
            (name, description, actor),
        )
    write_audit_event("cli", _actor(), "namespace_created", name, {"description": description})
    _output.ok(f"Namespace '{name}' created")


@app.command("info")
def ns_info(
    name: str = typer.Argument(..., help="Namespace name"),
) -> None:
    """Show namespace details and the models assigned to it."""
    _ensure_namespace_tables()
    with get_db() as conn:
        ns_row = conn.execute(
            "SELECT name, description, created_at, created_by FROM namespaces WHERE name=?",
            (name,),
        ).fetchone()
        if not ns_row:
            _output.error(f"Namespace '{name}' not found")
            raise typer.Exit(1)
        model_rows = conn.execute(
            "SELECT model, assigned_at FROM namespace_models WHERE namespace=? ORDER BY model",
            (name,),
        ).fetchall()

    if _output.json_mode:
        _output.print_json(
            {
                "name": ns_row["name"],
                "description": ns_row["description"],
                "created_at": ns_row["created_at"],
                "created_by": ns_row["created_by"],
                "models": [
                    {"model": r["model"], "assigned_at": r["assigned_at"]} for r in model_rows
                ],
            }
        )
        return

    _output.print_table(
        f"Namespace: {name}",
        ["Field", "Value"],
        [
            ["Name", ns_row["name"]],
            ["Description", ns_row["description"] or ""],
            ["Created at", ns_row["created_at"]],
            ["Created by", ns_row["created_by"] or ""],
            ["Models", str(len(model_rows))],
        ],
    )
    if model_rows:
        _output.print_table(
            "Assigned models",
            ["Model", "Assigned at"],
            [[r["model"], r["assigned_at"]] for r in model_rows],
        )
    else:
        _output.warning("No models assigned to this namespace.")


@app.command("assign")
def ns_assign(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    namespace: str = typer.Option(..., "--namespace", "-n", help="Target namespace name"),
) -> None:
    """Assign a model to a namespace."""
    _ensure_namespace_tables()
    with get_db() as conn:
        ns_row = conn.execute("SELECT name FROM namespaces WHERE name=?", (namespace,)).fetchone()
        if not ns_row:
            _output.error(
                f"Namespace '{namespace}' not found. Create it first with: exa namespace create {namespace}"
            )
            raise typer.Exit(1)
        conn.execute(
            "INSERT OR REPLACE INTO namespace_models (model, namespace) VALUES (?,?)",
            (model, namespace),
        )
    write_audit_event(
        "cli",
        _actor(),
        "namespace_model_assigned",
        model,
        {"namespace": namespace},
    )
    _output.ok(f"Model {model} assigned to namespace {namespace}")
