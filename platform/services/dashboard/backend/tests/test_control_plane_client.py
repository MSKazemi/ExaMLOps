import httpx
import pytest
from control_plane_client import ControlPlaneClient


@pytest.fixture
def client_factory():
    def _factory(handler):
        transport = httpx.MockTransport(handler)
        return ControlPlaneClient(base_url="http://cp", transport=transport)
    return _factory


async def test_get_meta(client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models/JPCP/meta"
        return httpx.Response(200, json={
            "name": "JPCP", "task_type": "regression",
            "estimator_class": "x", "supported_datasets": ["A"],
            "input_schema": {}, "output_schema": {},
            "promotion": {"metric": "rmse", "direction": "lower_is_better"},
            "path_in_repo": "modelzoo/.../jpcp/",
            "bundled_images": [],
        })
    cp = client_factory(handler)
    meta = await cp.get_meta("JPCP")
    assert meta["name"] == "JPCP"


async def test_get_meta_404_raises_keyerror(client_factory):
    def handler(_request):
        return httpx.Response(404, json={"detail": "Unknown model"})
    cp = client_factory(handler)
    with pytest.raises(KeyError):
        await cp.get_meta("Nope")


async def test_get_readme(client_factory):
    def handler(_request):
        return httpx.Response(200, json={"text": "# JPCP", "sha": "a" * 64})
    cp = client_factory(handler)
    text, sha = await cp.get_readme("JPCP")
    assert text == "# JPCP"
    assert sha == "a" * 64


async def test_list_model_names(client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models"
        return httpx.Response(200, json=[
            {"model_name": "JPCP", "datasets": ["A"]},
            {"model_name": "MACK", "datasets": ["B"]},
        ])
    cp = client_factory(handler)
    names = await cp.list_model_names()
    assert names == ["JPCP", "MACK"]


async def test_get_retries_transient_connect_timeout():
    """A transient ConnectTimeout on the first attempt is retried, then succeeds.

    This is the exact failure that blanked the Models page: the single-worker
    control plane briefly refuses a connection under load.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("transient", request=request)
        return httpx.Response(200, json=[{"model_name": "JPCP", "datasets": ["A"]}])

    transport = httpx.MockTransport(handler)
    cp = ControlPlaneClient(base_url="http://cp", transport=transport, backoff=0.0)
    names = await cp.list_model_names()
    assert names == ["JPCP"]
    assert calls["n"] == 2  # failed once, retried once, succeeded


async def test_get_raises_after_exhausting_retries():
    """Persistent transport failure surfaces after all retries are exhausted."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("always down", request=request)

    transport = httpx.MockTransport(handler)
    cp = ControlPlaneClient(base_url="http://cp", transport=transport, retries=2, backoff=0.0)
    with pytest.raises(httpx.ConnectTimeout):
        await cp.list_model_names()
    assert calls["n"] == 3  # initial + 2 retries
