"""Pure functions to build external-resource URLs for a model.

Returns only the keys whose inputs are non-None — the frontend hides
buttons whose key is absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass
class LinkInputs:
    model_name: str
    mlflow_model_id: str
    model_path_in_repo: str
    primary_dataset: str | None
    run_id: str | None
    version: str | None
    paper_url: str | None
    public_mlflow_url: str
    public_prefect_url: str
    public_ray_serve_url: str
    grafana_loki_explore_url: str
    public_control_plane_url: str
    examlops_repo_url: str | None
    examlops_repo_branch: str


def build_links(inp: LinkInputs) -> dict[str, str]:
    out: dict[str, str] = {}

    out["mlflow_model"] = f"{inp.public_mlflow_url}/#/models/{quote(inp.mlflow_model_id, safe='')}"
    if inp.run_id:
        out["mlflow_run"] = f"{inp.public_mlflow_url}/#/experiments/0/runs/{quote(inp.run_id, safe='')}"

    out["ray_serve_api"] = f"{inp.public_ray_serve_url}/docs#/default/predict_predict__name__post"

    if inp.primary_dataset:
        flow_name = f"{inp.model_name}_{inp.primary_dataset}_training_flow"
        out["prefect_flow"] = (
            f"{inp.public_prefect_url}/flow-runs?name={quote(flow_name, safe='')}"
        )

    if inp.examlops_repo_url:
        repo = inp.examlops_repo_url.rstrip("/")
        out["git_source"] = f"{repo}/blob/{inp.examlops_repo_branch}/{inp.model_path_in_repo}"

    if inp.paper_url:
        out["paper"] = inp.paper_url

    loki_query = '{service="ray-serve"} |= "model=' + inp.model_name + '"'
    panel = (
        '{"queries":[{"refId":"A","expr":"'
        + loki_query.replace('"', '\\\\"')
        + '"}]}'
    )
    out["loki_logs"] = f"{inp.grafana_loki_explore_url}?left=" + quote(panel, safe="")

    out["control_plane_api"] = f"{inp.public_control_plane_url}/docs#/default/retrain_retrain_post"

    return out
