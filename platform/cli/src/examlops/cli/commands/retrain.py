from __future__ import annotations

import os

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._enums import StorageBackend

_EXAMPLES = (
    "Examples:\n\n"
    "  # Fast dev-safe retrain (no data download)\n"
    "  exa retrain JPCP --dummy\n\n"
    "  # Production-style retrain via the Control Plane\n"
    "  exa retrain JPCP --dataset PM100Dataset --backend minio\n\n"
    "  # Specify dataset explicitly\n"
    "  exa retrain JPCP --dataset PM100Dataset\n\n"
    "  # Preview without triggering\n"
    "  exa retrain JPCP --dry-run\n\n"
    "  # Non-interactive (CI): skip the confirmation prompt\n"
    "  exa --yes retrain JPCP --dummy"
)


def retrain(
    model: str = typer.Argument(..., help="Model ID (e.g. JPCP)"),
    dataset: str | None = typer.Option(None, "--dataset", "-d", help="Dataset class name"),
    dummy: bool = typer.Option(False, "--dummy", help="Use dummy data (dev-safe, no downloads)"),
    backend: StorageBackend | None = typer.Option(None, "--backend", help="Storage backend"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be scheduled without triggering it"
    ),
) -> None:
    """Trigger a Prefect training run via the Control Plane."""
    cfg = load_config()
    dataset_name = dataset or "PM100Dataset"
    backend_name = backend.value if backend is not None else None
    body = {
        "model_name": model,
        "dataset_name": dataset_name,
        "is_dummy": dummy,
        "backend_name": backend_name,
    }

    # ── Dry run: describe the action, change nothing ──────────────────────────
    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "would_schedule": body})
        else:
            _output.info("Dry run — no retrain will be scheduled.")
            _output.print_record(
                {
                    "model": model,
                    "dataset": dataset_name,
                    "dummy": dummy,
                    "backend": backend_name or "default",
                }
            )
        return

    # ── Confirm the mutation (auto-yes under --yes / --json / CI) ──────────────
    if not _output.confirm(
        f"Schedule a retrain of {model} on {dataset_name}{' (dummy)' if dummy else ''}?",
        default=True,
    ):
        _output.warning("Aborted — no retrain scheduled.")
        raise typer.Exit(0)

    with _output.spinner(f"Scheduling retrain for {model}…"):
        try:
            result = _client.post(
                f"{cfg.control_plane_url}/retrain",
                body,
                token=cfg.control_plane_token,
            )
        except _client.ClientError as e:
            _output.error(
                f"Failed to schedule retrain for {model}: {e}",
                hint="Is the control plane running? Try: exa status",
            )
            return

    _record_audit(model, dataset_name, dummy, backend_name, result)

    _output.ok(f"Retrain scheduled for [bold]{model}[/bold] (dataset: {dataset_name})")
    _output.print_record(
        {
            "flow_run_id": result.get("flow_run_id", "—"),
            "model": model,
            "dataset": dataset_name,
            "dummy": dummy,
        }
    )
    _output.hint("Monitor: exa status  |  Watch logs: exa stack logs --service prefect")
    if _output.json_mode:
        _output.print_json(result)


def _record_audit(model: str, dataset: str, dummy: bool, backend: str | None, result: dict) -> None:
    """Write a best-effort audit event — never fail the command on audit errors."""
    try:
        from examlops.platform_db import write_audit_event

        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
        write_audit_event(
            source="exa-retrain",
            actor=actor,
            action="retrain_triggered",
            target=model.upper(),
            details={
                "dataset": dataset,
                "dummy": dummy,
                "backend": backend,
                "flow_run_id": result.get("flow_run_id"),
            },
        )
    except Exception:
        pass
