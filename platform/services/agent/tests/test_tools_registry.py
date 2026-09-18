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
    respx.get("http://localhost:18002/v1/models").mock(
        return_value=httpx.Response(
            200, json=[{"model_name": "JPCP", "datasets": ["PM100Dataset"]}]
        )
    )
    out = registry.list_datasets.invoke({})
    assert "JPCP" in out and "PM100Dataset" in out


@respx.mock
def test_list_models_finds_a_model_past_the_first_page():
    """Asking about one model must not depend on where it sits in the registry listing.

    The tool fetched a page of registered models and then matched the name in Python, so on a
    registry with more models than the page holds, a question about a later one answered "No
    models found in the registry." — indistinguishable from the model not existing, to a caller
    who cannot check. `examlops.serving_snapshot` calls this "the 100-model bug" and already
    follows the token; this tool did not.
    """
    pages = [
        {
            "registered_models": [
                {"name": f"filler{i}", "aliases": [], "latest_versions": []} for i in range(100)
            ],
            "next_page_token": "p2",
        },
        {
            "registered_models": [
                {
                    "name": "jpcp",
                    "aliases": [{"alias": "Production", "version": "3"}],
                    "latest_versions": [{"version": "3", "current_stage": "None"}],
                }
            ]
        },
    ]

    def _respond(request):
        token = request.url.params.get("page_token")
        return httpx.Response(200, json=pages[1 if token else 0])

    respx.get("http://localhost:15000/api/2.0/mlflow/registered-models/search").mock(
        side_effect=_respond
    )
    out = registry.list_models.invoke({"model_name": "JPCP"})
    assert "jpcp" in out and "Production=v3" in out, out
