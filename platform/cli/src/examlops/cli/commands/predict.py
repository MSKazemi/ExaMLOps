from __future__ import annotations

import json

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._enums import MLflowAlias

_EXAMPLES = (
    "Examples:\n\n"
    '  exa predict JPCP --features "$(python3 -c '
    '\'import json; print(json.dumps({"embedding":[0.1]*384,"num_nodes": 4,"user_id": "smoke"}))\''
    ')"\n\n'
    '  exa predict JPCP --features "$(python3 -c '
    '\'import json; print(json.dumps({"embedding":[0.1]*384,"num_nodes": 4,"user_id": "smoke"}))\''
    ')" --alias Canary\n\n'
    "  # The inference pipeline requires a 384-dim embedding and num_nodes."
)


def predict(
    model: str = typer.Argument(..., help="Model ID (e.g. JPCP)"),
    features: str = typer.Option(..., "--features", "-f", help="JSON feature dict"),
    alias: MLflowAlias | None = typer.Option(None, "--alias", help="MLflow alias"),
    version: str | None = typer.Option(None, "--version", help="Explicit model version"),
):
    """Send an inference request to the Ray Serve inference pipeline."""
    cfg = load_config()
    try:
        feat_dict = json.loads(features)
    except json.JSONDecodeError as e:
        _output.error(f"--features must be valid JSON: {e}")
        return
    if not isinstance(feat_dict, dict):
        _output.error("--features must be a JSON object")
        return
    body: dict = {**feat_dict, "model_name": model}
    if alias:
        body["alias"] = alias
    if version:
        body["version"] = version
    try:
        result = _client.post(f"{cfg.ray_serve_url}/infer-pipeline/infer", body)
    except _client.ClientError as e:
        _output.error(str(e))
        return
    _output.print_record(result)
