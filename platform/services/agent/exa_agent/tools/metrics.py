from __future__ import annotations

from datetime import UTC, datetime

import httpx
from langchain_core.tools import tool

from exa_agent import config
from exa_agent.tools import _http


@tool
def get_metrics(query: str = "examlops_predict_requests_total") -> str:
    """Run a PromQL query against Prometheus and return one line per result.

    Args:
        query: PromQL expression. Examples: 'examlops_models_loaded',
            'rate(examlops_predict_latency_seconds_sum[5m]) / rate(examlops_predict_latency_seconds_count[5m])'.
    """
    data, err = _http.request_json(
        "prometheus", "GET", f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": query}
    )
    if err:
        return err
    results = data.get("data", {}).get("result", [])
    if not results:
        return f"No data found for query: {query}"
    lines = []
    for r in results:
        metric = {k: v for k, v in r.get("metric", {}).items() if k != "__name__"}
        label = ", ".join(f"{k}={v}" for k, v in metric.items()) or query
        lines.append(f"  {label}: {r.get('value', ['?', '?'])[1]}")
    return f"Metrics ({query}):\n" + "\n".join(lines)


def _check_health(name: str, url: str) -> str:
    """Return 'up' or 'DOWN — <reason>' for a single health endpoint."""
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return "up"
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        return f"DOWN — {exc}"


@tool
def platform_health() -> str:
    """Report reachability/health of the control plane, Ray Serve, and MLflow."""
    checks = {
        "control_plane": f"{config.CONTROL_PLANE_URL}/health",
        "ray_serve": f"{config.RAY_SERVE_URL}/health",
        "mlflow": f"{config.MLFLOW_URL}/health",
    }
    lines = []
    for name, url in checks.items():
        status = _check_health(name, url)
        lines.append(f"  {name}: {status}")
    return "Platform health:\n" + "\n".join(lines)


@tool
def generate_report() -> str:
    """Generate a timestamped Markdown platform status report (models + health + metrics)."""
    from exa_agent.tools.approvals import list_pending_approvals
    from exa_agent.tools.registry import list_models

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    models = list_models.invoke({"model_name": ""})
    health = platform_health.invoke({})
    requests = get_metrics.invoke({"query": "examlops_predict_requests_total"})
    pending = list_pending_approvals.invoke({})
    return (
        f"## ExaMLOps Status Report — {now}\n\n"
        f"### Registered Models\n{models}\n\n"
        f"### {health}\n\n"
        f"### Request Counts\n{requests}\n\n"
        f"### Pending Approvals\n{pending}\n"
    )


TOOLS = [get_metrics, platform_health, generate_report]
