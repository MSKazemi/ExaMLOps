import httpx
import respx
from exa_agent.tools import metrics


@respx.mock
def test_get_metrics_formats_results():
    respx.get("http://localhost:19090/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"result": [{"metric": {"model": "jpcp"}, "value": [0, "4821"]}]}},
        )
    )
    out = metrics.get_metrics.invoke({"query": "examlops_predict_requests_total"})
    assert "jpcp" in out and "4821" in out


@respx.mock
def test_platform_health_aggregates():
    respx.get("http://localhost:18002/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get("http://localhost:18001/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get("http://localhost:15000/health").mock(return_value=httpx.Response(200, text="OK"))
    out = metrics.platform_health.invoke({})
    assert "control_plane" in out and "ray_serve" in out and "mlflow" in out
