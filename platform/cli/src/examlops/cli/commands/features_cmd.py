from __future__ import annotations

import os
import shutil
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.data import get_db, init_db

app = typer.Typer(
    help="Feature store — versioned training features",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_PUSH = (
    "Examples:\n\n"
    "  exa features push JPCP features.parquet\n\n"
    "  exa features push JPCP my_feats.csv --name engineered"
)
_EXAMPLES_LIST = "Examples:\n\n  exa features list\n\n  exa features list JPCP"
_EXAMPLES_PULL = (
    "Examples:\n\n"
    "  exa features pull JPCP\n\n"
    "  exa features pull JPCP --name engineered --version 2 --output ./local_feats.parquet"
)


def _db_path() -> str:
    from examlops.platform_db import _db_path as _p

    return _p()


def _store_dir() -> Path:
    return Path(os.getenv("FEATURE_STORE_DIR", str(Path(_db_path()).parent / ".feature_store")))


def _init_feature_table() -> None:
    init_db()
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feature_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model       TEXT NOT NULL,
                name        TEXT NOT NULL DEFAULT 'default',
                version     INTEGER NOT NULL DEFAULT 1,
                local_path  TEXT,
                size_bytes  INTEGER,
                schema_json TEXT,
                actor       TEXT
            );
        """)


@app.command("push", epilog=_EXAMPLES_PUSH)
def features_push(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    file_path: str = typer.Argument(..., help="Path to feature file to store"),
    name: str = typer.Option("default", "--name", "-n", help="Feature set name"),
) -> None:
    """Push a feature file into the versioned feature store."""
    _init_feature_table()

    src = Path(file_path)
    if not src.exists():
        _output.error(f"File not found: {file_path}")

    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS max_v FROM feature_versions WHERE model=? AND name=?",
            (model, name),
        ).fetchone()
        version = (row["max_v"] or 0) + 1

    store_dir = _store_dir()
    dest = store_dir / model / name / f"v{version}_{src.name}"
    os.makedirs(dest.parent, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    size_bytes = dest.stat().st_size

    actor = os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))

    with get_db() as conn:
        conn.execute(
            "INSERT INTO feature_versions (model, name, version, local_path, size_bytes, actor)"
            " VALUES (?,?,?,?,?,?)",
            (model, name, version, str(dest), size_bytes, actor),
        )

    _output.ok(f"Pushed {model}/{name} v{version} ({size_bytes} bytes)")


@app.command("list", epilog=_EXAMPLES_LIST)
def features_list(
    model: str | None = typer.Argument(None, help="Filter by model name"),
) -> None:
    """List feature versions in the store."""
    _init_feature_table()

    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT ts, model, name, version, size_bytes, local_path"
                " FROM feature_versions WHERE model=? ORDER BY ts DESC LIMIT 50",
                (model,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, model, name, version, size_bytes, local_path"
                " FROM feature_versions ORDER BY ts DESC LIMIT 50",
            ).fetchall()

    if not rows:
        _output.warning("No feature versions found")
        return

    records = [dict(r) for r in rows]

    if _output.json_mode:
        _output.print_json(records)
        return

    _output.print_table(
        "Feature Versions",
        ["Time", "Model", "Name", "Version", "Size", "Path"],
        [
            [
                r["ts"],
                r["model"],
                r["name"],
                str(r["version"]),
                str(r["size_bytes"]) + " B" if r["size_bytes"] is not None else "-",
                r["local_path"] or "-",
            ]
            for r in records
        ],
    )


@app.command("pull", epilog=_EXAMPLES_PULL)
def features_pull(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    name: str = typer.Option("default", "--name", "-n", help="Feature set name"),
    version: int | None = typer.Option(
        None, "--version", "-v", help="Specific version (default: latest)"
    ),
    output: str | None = typer.Option(None, "--output", "-o", help="Copy file to this path"),
) -> None:
    """Pull a feature file from the store."""
    _init_feature_table()

    with get_db() as conn:
        if version is not None:
            row = conn.execute(
                "SELECT ts, model, name, version, local_path, size_bytes"
                " FROM feature_versions WHERE model=? AND name=? AND version=?",
                (model, name, version),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT ts, model, name, version, local_path, size_bytes"
                " FROM feature_versions WHERE model=? AND name=?"
                " ORDER BY version DESC LIMIT 1",
                (model, name),
            ).fetchone()

    if row is None:
        _output.error("No feature version found")

    record = dict(row)

    if output:
        src = record["local_path"]
        if not src or not Path(src).exists():
            _output.error(f"Stored file not found on disk: {src}")
        shutil.copy2(src, output)
        _output.ok(f"Copied to {output}")
    else:
        if _output.json_mode:
            _output.print_json(record)
        else:
            _output.print_table(
                f"Feature: {model}/{name}",
                ["Model", "Name", "Version", "Size", "Path", "Stored At"],
                [
                    [
                        record["model"],
                        record["name"],
                        str(record["version"]),
                        str(record["size_bytes"]) + " B"
                        if record["size_bytes"] is not None
                        else "-",
                        record["local_path"] or "-",
                        record["ts"],
                    ]
                ],
            )
