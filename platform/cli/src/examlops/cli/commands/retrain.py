from __future__ import annotations

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._enums import StorageBackend

_EXAMPLES = (
    "Examples:\n\n"
    "  # Trigger retraining with the default dataset (PM100Dataset)\n"
    "  exa retrain JPCP\n\n"
    "  # Fast dev-safe retrain through the Control Plane\n"
    "  exa retrain JPCP --dataset PM100Dataset --dummy\n\n"
    "  # Production-style retrain against MinIO-backed data\n"
    "  exa retrain JPCP --dataset PM100Dataset --backend minio"
)


def retrain(
    model: str = typer.Argument(..., help="Model ID (e.g. JPCP)"),
    dataset: str | None = typer.Option(None, "--dataset", "-d", help="Dataset class name"),
    dummy: bool = typer.Option(False, "--dummy", help="Use dummy data (dev-safe)"),
    backend: StorageBackend | None = typer.Option(None, "--backend", help="Storage backend"),
):
    """Trigger a Prefect training run via the Control Plane."""
    cfg = load_config()
    body = {
        "model_name": model,
        "dataset_name": dataset or "PM100Dataset",
        "is_dummy": dummy,
        "backend_name": backend,
    }
    try:
        result = _client.post(f"{cfg.control_plane_url}/retrain", body, token=cfg.control_plane_token)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    _output.ok(f"Retrain scheduled — flow_run_id: {result['flow_run_id']}")
    if _output.json_mode:
        _output.print_json(result)
