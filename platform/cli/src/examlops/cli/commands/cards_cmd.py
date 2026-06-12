from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.platform_db import get_db, init_db, write_audit_event

app = typer.Typer(
    help="Generate model cards",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_CARD = (
    "Examples:\n\n"
    "  exa models card JPCP\n\n"
    "  exa models card JPCP --output /tmp/jpcp-card.md\n\n"
    "  exa models card JPCP --output ./cards/jpcp.md"
)

_EXAMPLES_HISTORY = (
    "Examples:\n\n"
    "  exa models card history JPCP\n\n"
    "  exa models card history\n\n"
    "  exa --json models card history JPCP"
)


def _ensure_model_cards_table() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model       TEXT NOT NULL,
                output_path TEXT,
                actor       TEXT
            )
        """)


def _record_card(model: str, output_path: str | None) -> None:
    actor = os.getenv("EXAMLOPS_ACTOR", os.getenv("USER", "unknown"))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO model_cards (model, output_path, actor) VALUES (?,?,?)",
            (model, output_path, actor),
        )


def _format_lifecycle_table(lifecycle_gates: list | dict) -> str:
    """Format lifecycle gates as a markdown table."""
    if not lifecycle_gates:
        return "_No lifecycle gates configured._"
    if isinstance(lifecycle_gates, dict):
        items = list(lifecycle_gates.items())
        if not items:
            return "_No lifecycle gates configured._"
        lines = ["| Stage | Config |", "|---|---|"]
        for stage, cfg in items:
            lines.append(f"| {stage} | {cfg} |")
        return "\n".join(lines)
    if isinstance(lifecycle_gates, list):
        if not lifecycle_gates:
            return "_No lifecycle gates configured._"
        # list of dicts
        if isinstance(lifecycle_gates[0], dict):
            keys = list(lifecycle_gates[0].keys())
            header = "| " + " | ".join(keys) + " |"
            sep = "|" + "|".join("---|" for _ in keys)
            lines = [header, sep]
            for entry in lifecycle_gates:
                row = "| " + " | ".join(str(entry.get(k, "")) for k in keys) + " |"
                lines.append(row)
            return "\n".join(lines)
        # plain list
        return "\n".join(f"- {item}" for item in lifecycle_gates)
    return "_No lifecycle gates configured._"


def _format_versions_table(versions: list) -> str:
    """Format MLflow model versions as a markdown table."""
    if not versions:
        return "_No versions found._"
    lines = ["| Version | Stage | Run ID |", "|---|---|---|"]
    for v in versions[:5]:
        ver = v.get("version", "—")
        stage = v.get("current_stage", v.get("aliases", "—"))
        run_id = (v.get("run_id") or "—")[:8]
        lines.append(f"| {ver} | {stage} | {run_id} |")
    return "\n".join(lines)


def _build_markdown(model: str, meta: dict, mlflow_data: dict) -> str:
    """Generate the model card markdown document."""
    generated = datetime.utcnow().isoformat()

    task_type = meta.get("task_type") or "N/A"
    framework = meta.get("framework") or "N/A"
    enabled = meta.get("enabled", "N/A")

    datasets_raw = meta.get("datasets") or []
    if isinstance(datasets_raw, list):
        datasets_str = ", ".join(datasets_raw) if datasets_raw else "N/A"
    else:
        datasets_str = str(datasets_raw) if datasets_raw else "N/A"

    lifecycle_gates = meta.get("lifecycle_gates") or meta.get("lifecycle") or []
    lifecycle_section = _format_lifecycle_table(lifecycle_gates)

    rm = mlflow_data.get("registered_model", {})
    versions = rm.get("latest_versions", [])
    versions_section = _format_versions_table(versions)

    return (
        f"# Model Card: {model}\n\n"
        f"**Generated:** {generated}\n\n"
        f"## Overview\n\n"
        f"- Task: {task_type} | Framework: {framework} | Enabled: {enabled}\n\n"
        f"## Datasets\n\n"
        f"{datasets_str}\n\n"
        f"## Lifecycle Gates\n\n"
        f"{lifecycle_section}\n\n"
        f"## MLflow Versions\n\n"
        f"{versions_section}\n\n"
        f"## Usage\n\n"
        f"```bash\n"
        f"curl -X POST http://localhost:18001/predict/{model} -d '{{\"embedding\": [...]}}'\n"
        f"```\n"
    )


@app.command("generate", epilog=_EXAMPLES_CARD)
def generate(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write card to this file path instead of stdout"
    ),
) -> None:
    """Generate a standardised model card document."""
    cfg = load_config()
    init_db()
    _ensure_model_cards_table()

    # Fetch metadata from control plane
    meta: dict = {}
    meta_url = f"{cfg.control_plane_url}/models/{urllib.parse.quote(model)}/meta"
    try:
        meta = _client.get(meta_url)
    except _client.ClientError as exc:
        _output.error(
            f"Control plane unreachable or model not found: {exc}",
            hint="Is the control plane running? Try: exa status",
        )

    # Fetch MLflow registered model data
    mlflow_data: dict = {}
    mlflow_url = (
        f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get"
        f"?name={urllib.parse.quote(model.lower())}"
    )
    try:
        mlflow_data = _client.get(mlflow_url)
    except _client.ClientError:
        pass  # non-fatal — card still generated with available data

    markdown = _build_markdown(model, meta, mlflow_data)

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown)
        _record_card(model, str(out_path))
        write_audit_event(
            source="exa-cli",
            actor=os.getenv("EXAMLOPS_ACTOR", os.getenv("USER")),
            action="model_card_generated",
            target=model,
            details={"output_path": str(out_path)},
        )
        _output.ok(f"Model card written to {out_path}")
    else:
        _record_card(model, None)
        write_audit_event(
            source="exa-cli",
            actor=os.getenv("EXAMLOPS_ACTOR", os.getenv("USER")),
            action="model_card_generated",
            target=model,
            details={"output_path": None},
        )
        typer.echo(markdown)
        _output.ok("Model card generated")


@app.command("history", epilog=_EXAMPLES_HISTORY)
def card_history(
    model: str | None = typer.Argument(None, help="Filter by model name (omit for all)"),
) -> None:
    """Show model card generation history."""
    init_db()
    _ensure_model_cards_table()

    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT ts, model, output_path FROM model_cards "
                "WHERE model=? ORDER BY ts DESC LIMIT 20",
                (model,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, model, output_path FROM model_cards ORDER BY ts DESC LIMIT 20",
            ).fetchall()

    if not rows:
        subject = f" for {model}" if model else ""
        _output.ok(f"No model card history{subject}.")
        return

    table_rows = [[r["ts"], r["model"], r["output_path"] or "(stdout)"] for r in rows]
    _output.print_table(
        "Model Card History",
        ["Time", "Model", "Output Path"],
        table_rows,
    )
