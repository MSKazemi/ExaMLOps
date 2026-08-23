from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import typer

from examlops.cli import _output
from examlops.cli._config import load_config
from examlops.data import get_db, init_db
from examlops.data.audit import write_audit_event

app = typer.Typer(
    help="Roll back a model alias to a previous version.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_ROLLBACK = (
    "Examples:\n\n"
    "  exa models rollback run JPCP --version 5\n\n"
    "  exa models rollback run JPCP --version 5 --alias Staging\n\n"
    "  exa models rollback run JPCP --version 5 --dry-run\n\n"
    "  exa --yes models rollback run JPCP --version 5"
)

_EXAMPLES_HISTORY = (
    "Examples:\n\n  exa models rollback history JPCP\n\n  exa --json models rollback history JPCP"
)

_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS model_rollbacks ("
    "  id           INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
    "  model        TEXT NOT NULL,"
    "  from_version INTEGER,"
    "  to_version   INTEGER NOT NULL,"
    "  alias        TEXT NOT NULL DEFAULT 'Production',"
    "  actor        TEXT,"
    "  reason       TEXT"
    ")"
)


def _ensure_table(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)


def _fetch_versions(cfg, model: str) -> list[dict]:
    """Return all model versions from MLflow for *model* (lowercase)."""
    import urllib.parse

    encoded = urllib.parse.quote(model.lower())
    url = (
        f"{cfg.mlflow_url}/api/2.0/mlflow/model-versions/search"
        f"?max_results=50&filter=name%3D%27{encoded}%27"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as exc:
        _output.error(f"MLflow unreachable: {exc}", hint="Is MLflow running? Try: exa status")
    return data.get("model_versions", [])


def _get_current_alias_version(cfg, model: str, alias: str) -> int | None:
    """Return the version number currently pointing to *alias*, or None."""
    import urllib.parse

    encoded = urllib.parse.quote(model.lower())
    url = f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get?name={encoded}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.URLError:
        return None
    aliases = {
        a["alias"]: int(a["version"]) for a in data.get("registered_model", {}).get("aliases", [])
    }
    return aliases.get(alias)


def _set_alias(cfg, model: str, alias: str, version: int) -> None:
    """POST to MLflow to set *alias* → *version* for *model*."""
    body = json.dumps(
        {
            "name": model.lower(),
            "alias": alias,
            "version": str(version),
        }
    ).encode()
    req = urllib.request.Request(
        f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/alias",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read())
    except urllib.error.URLError as exc:
        _output.error(
            f"MLflow unreachable when setting alias: {exc}",
            hint="Is MLflow running? Try: exa status",
        )


@app.command("run", epilog=_EXAMPLES_ROLLBACK)
def rollback(
    model: str = typer.Argument(..., help="Registered model name (e.g. JPCP)"),
    version: int | None = typer.Option(
        None, "--version", "-v", help="Target version number to roll back to"
    ),
    alias: str = typer.Option(
        "Production", "--alias", "-a", help="Alias to reassign (default: Production)"
    ),
    reason: str | None = typer.Option(
        None, "--reason", "-r", help="Optional reason for the rollback"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Preview without applying the change"
    ),
) -> None:
    """Roll back a model alias (default: Production) to a specified or selected version."""
    cfg = load_config()
    init_db()

    # Fetch available versions
    versions = _fetch_versions(cfg, model)
    if not versions:
        _output.error(f"No versions found for model '{model}' in MLflow")

    # Sort descending by version number
    versions_sorted = sorted(versions, key=lambda v: int(v["version"]), reverse=True)

    # Determine the current version for the alias
    current_version = _get_current_alias_version(cfg, model, alias)

    if version is None:
        if _output.yes_mode:
            # In --yes mode: pick latest-1 (second most recent version)
            candidates = [int(v["version"]) for v in versions_sorted]
            if current_version is not None:
                candidates = [v for v in candidates if v != current_version]
            if not candidates:
                _output.error(f"No previous version available to roll back to for '{model}'")
            version = candidates[0]
            _output.info(f"Auto-selected version {version} (--yes mode)")
        else:
            # Interactive: show available versions and prompt
            _output.console.print(f"\n[bold cyan]Available versions for {model}:[/bold cyan]")
            rows = []
            for v in versions_sorted:
                ver_num = int(v["version"])
                current_marker = f" <- {alias}" if ver_num == current_version else ""
                rows.append(
                    [
                        str(ver_num),
                        v.get("current_stage", "—"),
                        v.get("status", "—"),
                        current_marker,
                    ]
                )
            _output.print_table(
                f"Versions — {model}",
                ["Version", "Stage", "Status", "Note"],
                rows,
            )
            version = typer.prompt(f"Enter version number to roll back {alias} to", type=int)

    # Sanity check
    if current_version == version:
        _output.warning(f"Alias '{alias}' already points to version {version} — nothing to do")
        return

    if dry_run:
        _output.console.print(
            f"[yellow]dry-run[/yellow] Would roll back [bold]{model}[/bold] "
            f"{alias}: v{current_version} -> v{version}"
        )
        return

    if not _output.confirm(f"Roll back {model} {alias}: v{current_version} -> v{version}?"):
        _output.info("Aborted")
        return

    # Apply the alias change in MLflow
    _set_alias(cfg, model, alias, version)

    # Record in DB
    actor = os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))
    with get_db() as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO model_rollbacks "
            "(model, from_version, to_version, alias, actor, reason) "
            "VALUES (?,?,?,?,?,?)",
            (model.upper(), current_version, version, alias, actor, reason),
        )

    write_audit_event(
        source="exa-models-rollback",
        actor=actor,
        action="rollback",
        target=model.upper(),
        details={
            "alias": alias,
            "from_version": current_version,
            "to_version": version,
            "reason": reason,
        },
    )

    _output.ok(
        f"Rolled back {model} {alias}: "
        f"v{current_version} -> v{version}" + (f" (reason: {reason})" if reason else "")
    )


@app.command("history", epilog=_EXAMPLES_HISTORY)
def rollback_history(
    model: str = typer.Argument(..., help="Registered model name (e.g. JPCP)"),
) -> None:
    """Show rollback history for a model (last 20 events)."""
    init_db()

    with get_db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT ts, model, from_version, to_version, alias, actor, reason "
            "FROM model_rollbacks WHERE model=? ORDER BY ts DESC LIMIT 20",
            (model.upper(),),
        ).fetchall()

    if not rows:
        _output.ok(f"No rollback history found for '{model}'")
        return

    if _output.json_mode:
        _output.print_json([dict(r) for r in rows])
        return

    table_rows = [
        [
            r["ts"][:19] if r["ts"] else "—",
            r["model"],
            str(r["from_version"]) if r["from_version"] is not None else "—",
            str(r["to_version"]),
            r["alias"],
            r["actor"] or "—",
            r["reason"] or "—",
        ]
        for r in rows
    ]
    _output.print_table(
        f"Rollback History — {model}",
        ["Time", "Model", "From", "To", "Alias", "Actor", "Reason"],
        table_rows,
    )
