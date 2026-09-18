"""The dashboard authenticates to MLflow and Prefect when they require it (plan P3.6)."""

from __future__ import annotations

import base64

import pytest


def _basic(value: str) -> str:
    return "Basic " + base64.b64encode(value.encode()).decode()


@pytest.mark.asyncio
async def test_the_proxy_injects_mlflow_and_prefect_credentials(monkeypatch):
    from routers import proxy

    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "pw")
    monkeypatch.setenv("PREFECT_API_AUTH_STRING", "svc:pf")

    mlflow = await proxy.INJECTORS["mlflow"]({"accept": "text/html"}, None)
    prefect = await proxy.INJECTORS["prefect"]({}, None)

    assert mlflow == {"accept": "text/html", "Authorization": _basic("svc:pw")}
    assert prefect == {"Authorization": _basic("svc:pf")}


@pytest.mark.asyncio
async def test_nothing_is_injected_while_auth_is_off(monkeypatch):
    from routers import proxy

    for var in (
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
        "MLFLOW_TRACKING_TOKEN",
        "PREFECT_API_AUTH_STRING",
        "PREFECT_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert await proxy.INJECTORS["mlflow"]({}, None) == {}
    assert await proxy.INJECTORS["prefect"]({}, None) == {}


def test_the_model_registry_client_and_the_pipeline_client_send_it(monkeypatch):
    from routers import models, pipelines

    monkeypatch.setenv("MLFLOW_TRACKING_USERNAME", "svc")
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "pw")
    monkeypatch.setenv("PREFECT_API_AUTH_STRING", "svc:pf")

    assert models._mlflow_client().headers["Authorization"] == _basic("svc:pw")
    assert pipelines._prefect_auth() == {"Authorization": _basic("svc:pf")}
