from __future__ import annotations

from langchain_core.tools import tool

from skipper import config
from skipper.tools import _http


@tool
def list_models(model_name: str = "") -> str:
    """List registered models in the MLflow registry with versions and active aliases.

    Args:
        model_name: Optional filter; empty returns all models.
    """
    # Follow `next_page_token` — the loop `examlops.serving_snapshot` documents as "the 100-model
    # bug, by design". Matching the name against one page means a question about a model listed
    # past it answers "No models found", which to an agent is indistinguishable from the model not
    # existing. The registry is also the thing that grows: retraining adds to it forever.
    models: list[dict] = []
    token: str | None = None
    seen: set[str] = set()
    while True:
        params: dict[str, object] = {"max_results": 1000}
        if token:
            params["page_token"] = token
        data, err = _http.request_json(
            "mlflow",
            "GET",
            f"{config.MLFLOW_URL}/api/2.0/mlflow/registered-models/search",
            params=params,
        )
        if err:
            return err
        models.extend(data.get("registered_models", []))
        token = data.get("next_page_token")
        if not token:
            break
        if token in seen:  # a token that repeats never advances
            return "MLflow repeated a registry page token; the model list may be incomplete."
        seen.add(token)
    if model_name:
        models = [m for m in models if m.get("name", "").upper() == model_name.upper()]
    if not models:
        return "No models found in the registry."
    lines = []
    for model in models:
        name = model.get("name", "?")
        aliases = {a["alias"]: a["version"] for a in model.get("aliases", []) if a.get("alias")}
        versions = (
            ", ".join(
                f"v{v.get('version')} ({v.get('current_stage', 'None')})"
                for v in model.get("latest_versions", [])
            )
            or "no versions"
        )
        alias_str = ", ".join(f"{k}=v{v}" for k, v in aliases.items()) or "none"
        lines.append(f"- {name}: versions=[{versions}] aliases=[{alias_str}]")
    return "\n".join(lines)


@tool
def describe_model(name: str) -> str:
    """Show metadata for one model: README summary, lifecycle stages, supported datasets.

    Args:
        name: Model name (e.g. 'JPCP').
    """
    meta, err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/models/{name}/meta"
    )
    if err:
        return err
    readme, _ = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/models/{name}/readme"
    )
    summary = (readme or {}).get("summary") or (meta or {}).get("summary") or "(no summary)"
    datasets = ", ".join((meta or {}).get("datasets", [])) or "unknown"
    stages = ", ".join((meta or {}).get("stages", [])) or "unknown"
    return f"{name}\n  summary: {summary}\n  datasets: {datasets}\n  stages: {stages}"


@tool
def list_datasets() -> str:
    """List the datasets each registered model supports."""
    data, err = _http.request_json("control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/models")
    if err:
        return err
    if not data:
        return "No models registered."
    return "\n".join(f"- {m.get('model_name')}: {', '.join(m.get('datasets', []))}" for m in data)


TOOLS = [list_models, describe_model, list_datasets]
