from __future__ import annotations

import time

import httpx

from exa_agent import config

# Feature 6: delay between retries — override in tests by patching this symbol
_REQUEST_RETRY_DELAY = 0.1


def _format_error(service: str, url: str, exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:300]
        return f"Error: {service} returned {exc.response.status_code}: {body}"
    return f"Error: cannot reach {service} at {url} — {exc}"


def request_json(service: str, method: str, url: str, *, retries: int = 2, **kwargs):
    """Return (data, None) on success or (None, error_string) on failure.

    Retries up to `retries` times on transient network errors (RequestError)
    with exponential back-off. HTTP errors (4xx/5xx) are returned immediately
    without retrying since they represent a definitive server response.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
                resp = client.request(method, url, **kwargs)
                resp.raise_for_status()
                if not resp.content:
                    return {}, None
                try:
                    return resp.json(), None
                except ValueError:
                    # Non-JSON 2xx body (e.g. a health endpoint returning "OK")
                    return resp.text, None
        except httpx.HTTPStatusError as exc:
            # Definitive server response — don't retry
            return None, _format_error(service, url, exc)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(_REQUEST_RETRY_DELAY * (2**attempt))
    return None, _format_error(service, url, last_exc)


class DashboardClient:
    """Authenticated client for dashboard-only endpoints (service control, pipelines, scaffold).

    The dashboard issues short-lived JWTs and has no service token, so we log in with
    the admin password, cache the JWT, and re-authenticate on a 401.
    """

    def __init__(self, base_url: str | None = None, password: str | None = None):
        self._base = (base_url or config.DASHBOARD_URL).rstrip("/")
        self._password = config.DASHBOARD_ADMIN_PASSWORD if password is None else password
        self._token: str | None = None

    def _login(self) -> None:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            resp = client.post(f"{self._base}/api/login", json={"password": self._password})
            resp.raise_for_status()
            self._token = resp.json()["token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def request(self, service: str, method: str, path: str, **kwargs):
        if not self._password:
            return (
                None,
                "Error: DASHBOARD_ADMIN_PASSWORD is not set — dashboard tools are disabled.",
            )
        url = f"{self._base}{path}"
        try:
            with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
                if self._token is None:
                    self._login()
                resp = client.request(method, url, headers=self._headers(), **kwargs)
                if resp.status_code == 401:
                    self._login()
                    resp = client.request(method, url, headers=self._headers(), **kwargs)
                resp.raise_for_status()
                return (resp.json() if resp.content else {}), None
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            return None, _format_error(service, url, exc)


_DASHBOARD: DashboardClient | None = None


def dashboard() -> DashboardClient:
    """Lazy singleton used by services.py and pipelines.py."""
    global _DASHBOARD
    if _DASHBOARD is None:
        _DASHBOARD = DashboardClient()
    return _DASHBOARD
