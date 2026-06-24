from unittest.mock import patch

import httpx
import respx
from exa_agent.tools import _http


@respx.mock
def test_request_json_success():
    respx.get("http://svc/x").mock(return_value=httpx.Response(200, json={"ok": 1}))
    data, err = _http.request_json("svc", "GET", "http://svc/x")
    assert err is None
    assert data == {"ok": 1}


@respx.mock
def test_request_json_non_json_body_returns_text():
    respx.get("http://svc/health").mock(return_value=httpx.Response(200, text="OK"))
    data, err = _http.request_json("svc", "GET", "http://svc/health")
    assert err is None
    assert data == "OK"


@respx.mock
def test_request_json_http_error():
    respx.get("http://svc/x").mock(return_value=httpx.Response(500, text="boom"))
    data, err = _http.request_json("svc", "GET", "http://svc/x")
    assert data is None
    assert "svc returned 500" in err


@respx.mock
def test_request_json_unreachable():
    respx.get("http://svc/x").mock(side_effect=httpx.ConnectError("refused"))
    data, err = _http.request_json("svc", "GET", "http://svc/x")
    assert data is None
    assert "cannot reach svc" in err


@respx.mock
def test_dashboard_client_logs_in_then_calls():
    respx.post("http://dash/api/login").mock(
        return_value=httpx.Response(200, json={"token": "T", "role": "admin", "expires_at": "x"})
    )
    route = respx.get("http://dash/api/containers").mock(
        return_value=httpx.Response(200, json=[{"name": "mlflow"}])
    )
    client = _http.DashboardClient(base_url="http://dash", password="pw")
    data, err = client.request("dashboard", "GET", "/api/containers")
    assert err is None and data == [{"name": "mlflow"}]
    assert route.calls.last.request.headers["authorization"] == "Bearer T"


@respx.mock
def test_dashboard_client_reauths_on_401():
    respx.post("http://dash/api/login").mock(
        return_value=httpx.Response(200, json={"token": "T2", "role": "admin", "expires_at": "x"})
    )
    respx.get("http://dash/api/containers").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[])]
    )
    client = _http.DashboardClient(base_url="http://dash", password="pw")
    client._token = "STALE"  # force the 401 path
    data, err = client.request("dashboard", "GET", "/api/containers")
    assert err is None and data == []


def test_dashboard_client_no_password():
    client = _http.DashboardClient(base_url="http://dash", password="")
    data, err = client.request("dashboard", "GET", "/api/containers")
    assert data is None
    assert "DASHBOARD_ADMIN_PASSWORD is not set" in err


# ── Feature 6: HTTP retry ─────────────────────────────────────────────────────


@respx.mock
def test_request_json_retries_network_error_and_succeeds():
    """RequestError on first attempt → retry → success on second."""
    route = respx.get("http://svc/retry")
    route.mock(side_effect=[httpx.ConnectError("flap"), httpx.Response(200, json={"ok": 1})])
    with patch.object(_http, "_REQUEST_RETRY_DELAY", 0):
        data, err = _http.request_json("svc", "GET", "http://svc/retry", retries=1)
    assert err is None
    assert data == {"ok": 1}


@respx.mock
def test_request_json_no_retry_on_http_error():
    """5xx HTTP response must NOT be retried — it's a definitive server response."""
    route = respx.get("http://svc/x5xx").mock(return_value=httpx.Response(503, text="overload"))
    data, err = _http.request_json("svc", "GET", "http://svc/x5xx", retries=2)
    assert data is None
    assert "svc returned 503" in err
    assert route.call_count == 1  # called exactly once, no retry


@respx.mock
def test_request_json_exhausts_retries():
    """All attempts fail → error returned after retries+1 total attempts."""
    route = respx.get("http://svc/flap").mock(side_effect=httpx.ConnectError("refused"))
    with patch.object(_http, "_REQUEST_RETRY_DELAY", 0):
        data, err = _http.request_json("svc", "GET", "http://svc/flap", retries=2)
    assert data is None
    assert "cannot reach svc" in err
    assert route.call_count == 3  # 1 initial + 2 retries
