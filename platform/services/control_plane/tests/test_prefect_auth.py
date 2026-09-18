"""The control plane's Prefect calls carry Prefect's auth string when one is set (plan P3.6)."""

from __future__ import annotations

import base64


def test_the_gateway_sends_the_auth_string(monkeypatch):
    from cplane import gateway

    monkeypatch.setenv("PREFECT_API_AUTH_STRING", "svc:pf-secret")
    client = gateway.PrefectGateway()._client()
    expected = "Basic " + base64.b64encode(b"svc:pf-secret").decode()
    assert client.headers["Authorization"] == expected


def test_without_an_auth_string_it_sends_none(monkeypatch):
    from cplane import gateway

    monkeypatch.delenv("PREFECT_API_AUTH_STRING", raising=False)
    monkeypatch.delenv("PREFECT_API_KEY", raising=False)
    assert "Authorization" not in gateway.PrefectGateway()._client().headers
