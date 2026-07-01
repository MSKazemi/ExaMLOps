from __future__ import annotations

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
    "  exa retrain JPCP --dataset PM100Dataset"
)


def retrain(
    model: str = typer.Argument(..., help="Model ID (e.g. JPCP)"),
    dataset: str | None = typer.Option(None, "--dataset", "-d", help="Dataset class name"),
    dummy: bool = typer.Option(False, "--dummy", help="Use dummy data (dev-safe, no downloads)"),
    backend: StorageBackend | None = typer.Option(None, "--backend", help="Storage backend"),
) -> None:
    """Trigger a Prefect training run via the Control Plane."""
    cfg = load_config()
    dataset_name = dataset or "PM100Dataset"
    body = {
        "model_name": model,
        "dataset_name": dataset_name,
        "is_dummy": dummy,
        "backend_name": backend,
    }
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
