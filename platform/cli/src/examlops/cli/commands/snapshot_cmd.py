"""``exa serve snapshot`` — the serving snapshot replicas act on (ADR 0127, plan P4.2)."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import typer

from examlops.cli import _output

app = typer.Typer(
    help="The serving snapshot: the alias→version, traffic and shadow view every replica serves.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES_SHOW = (
    "Examples:\n\n"
    "  # Which generation is current, and what it says each model serves\n"
    "  exa serve snapshot show\n\n"
    "  # The whole snapshot as JSON\n"
    "  exa serve snapshot show --json"
)
_EXAMPLES_PUBLISH = (
    "Examples:\n\n"
    "  # Compile from MLflow and the serving config now, publishing if anything changed\n"
    "  exa serve snapshot publish"
)


def _age(compiled_at: str | None) -> str:
    if not compiled_at:
        return "—"
    try:
        then = datetime.fromisoformat(compiled_at.replace("Z", "+00:00"))
    except ValueError:
        return compiled_at
    seconds = int((datetime.now(UTC) - then).total_seconds())
    return f"{seconds}s ago" if seconds < 120 else f"{seconds // 60}m ago"


@app.command("show", epilog=_EXAMPLES_SHOW)
def show() -> None:
    """Show the newest serving snapshot (generation, digest, per-model alias versions)."""
    from examlops.serving_snapshot import latest

    snapshot = latest()
    if snapshot is None:
        if _output.json_mode:
            _output.print_json(None)
            return
        _output.warning(
            "No serving snapshot has been published yet — replicas scan MLflow themselves. "
            "The control plane publishes one on start; `exa serve snapshot publish` does it now."
        )
        return
    if _output.json_mode:
        _output.print_json(snapshot)
        return
    _output.print_table(
        f"Serving snapshot — generation {snapshot['generation']}",
        ["Field", "Value"],
        [
            ["digest", str(snapshot.get("digest", ""))[:23] + "…"],
            ["compiled", _age(snapshot.get("compiled_at"))],
            ["models", str(len(snapshot.get("models", {})))],
            ["traffic splits", str(len(snapshot.get("traffic", {})))],
            ["shadow targets", str(len(snapshot.get("shadow", {})))],
        ],
    )
    rows = [
        [
            entry.get("name", key),
            ", ".join(
                f"{alias}=v{facts.get('version')}"
                for alias, facts in sorted(entry.get("aliases", {}).items())
            )
            or "—",
        ]
        for key, entry in sorted(snapshot.get("models", {}).items())
    ]
    if rows:
        _output.print_table("Models", ["Model", "Aliases"], rows)


@app.command("publish", epilog=_EXAMPLES_PUBLISH)
def publish() -> None:
    """Compile the snapshot from MLflow and the serving config and publish it if it changed."""
    from examlops.data.audit import write_audit_event
    from examlops.serving_snapshot import compile_and_publish

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    try:
        generation, published = compile_and_publish(actor=actor)
    except Exception as exc:  # noqa: BLE001 - the reason is the useful part
        _output.error(
            f"Could not compile the serving snapshot: {exc}",
            hint="The snapshot needs MLflow (MLFLOW_TRACKING_URI) and the platform database.",
        )
        return
    write_audit_event(
        "cli",
        actor,
        "serving_snapshot_published",
        None,
        {"generation": generation, "changed": published},
    )
    if _output.json_mode:
        _output.print_json({"generation": generation, "published": published})
        return
    if published:
        _output.ok(f"Published serving snapshot generation {generation}")
    else:
        _output.info(f"Nothing changed — generation {generation} is current")
