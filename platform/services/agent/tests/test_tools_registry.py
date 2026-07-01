import httpx
import respx
from skipper.tools import registry


@respx.mock
def test_list_models_formats_aliases():
    respx.get("http://localhost:15000/api/2.0/mlflow/registered-models/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "registered_models": [
                    {
                        "name": "jpcp",
                        "aliases": [{"alias": "Production", "version": "3"}],
                        "latest_versions": [{"version": "3", "current_stage": "None"}],
                    }
                ]
            },
        )
    )
    out = registry.list_models.invoke({"model_name": ""})
    assert "jpcp" in out and "Production=v3" in out


@respx.mock
def test_list_models_unreachable():
    respx.get("http://localhost:15000/api/2.0/mlflow/registered-models/search").mock(
        side_effect=httpx.ConnectError("x")
    )
    out = registry.list_models.invoke({"model_name": ""})
    assert "cannot reach mlflow" in out


@respx.mock
def test_list_datasets():
    respx.get("http://localhost:18002/models").mock(
        return_value=httpx.Response(
            200, json=[{"model_name": "JPCP", "datasets": ["PM100Dataset"]}]
        )
    )
    out = registry.list_datasets.invoke({})
    assert "JPCP" in out and "PM100Dataset" in out
