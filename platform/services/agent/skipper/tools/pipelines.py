from __future__ import annotations

from langchain_core.tools import tool

from skipper.confirm import confirmed_write
from skipper.tools import _http


@tool
def list_deployments() -> str:
    """List Prefect deployments (via the dashboard pipelines page)."""
    data, err = _http.dashboard().request("dashboard", "GET", "/api/pipelines/deployments")
    if err:
        return err
    return f"deployments: {data}"


@tool
def list_runs(limit: int = 20) -> str:
    """List recent Prefect flow runs, newest first (via the dashboard).

    Args:
        limit: Max runs to return.
    """
    data, err = _http.dashboard().request(
        "dashboard", "GET", "/api/pipelines/runs", params={"limit": limit}
    )
    if err:
        return err
    return f"recent runs: {data}"


@tool
def scaffold_preview(
    name: str, task: str = "performance_prediction", task_type: str = "regression"
) -> str:
    """Dry-run a new-model scaffold: returns the files that WOULD be generated (no writes).

    Args:
        name: Model name (e.g. 'DemoAD').
        task: Task slug (e.g. 'anomaly_detection').
        task_type: 'regression' or 'classification'.
    """
    data, err = _http.dashboard().request(
        "dashboard",
        "POST",
        "/api/scaffold/preview",
        json={"name": name, "task": task, "task_type": task_type},
    )
    if err:
        return err
    files = "\n".join(f"  {p}" for p in (data or {}).keys())
    return f"scaffold_preview for {name} would create:\n{files}"


@tool
@confirmed_write(
    lambda name, task="performance_prediction", task_type="regression": (
        f"Scaffold a new model '{name}' (task={task}, type={task_type}) — writes files into the repo"
    )
)
def scaffold_create(
    name: str, task: str = "performance_prediction", task_type: str = "regression"
) -> str:
    """Generate a new model's scaffold files in the repo (via the dashboard).

    Args:
        name: Model name (e.g. 'DemoAD').
        task: Task slug (e.g. 'anomaly_detection').
        task_type: 'regression' or 'classification'.
    """
    data, err = _http.dashboard().request(
        "dashboard",
        "POST",
        "/api/scaffold/create",
        json={"name": name, "task": task, "task_type": task_type},
    )
    if err:
        return err
    return f"scaffold_create: {(data or {}).get('message', data)}"


TOOLS = [list_deployments, list_runs, scaffold_preview, scaffold_create]
