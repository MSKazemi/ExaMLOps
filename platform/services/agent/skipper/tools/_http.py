from __future__ import annotations

import time

import httpx

from skipper import config

# Feature 6: delay between retries — override in tests by patching this symbol
_REQUEST_RETRY_DELAY = 0.1


def _format_error(service: str, url: str, exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:300]
        return f"Error: {service} returned {exc.response.status_code}: {body}"
    return f"Error: cannot reach {service} at {url} — {exc}"


def _with_control_plane_auth(service: str, url: str, kwargs: dict) -> dict:
    """Attach the control-plane bearer credential to a call that targets the control plane.

    Every control-plane route except the probes requires a ``read`` (or ``write``) scoped bearer.
    The registry, approvals and ModelZoo tools called it with none and got 401 once ``/models*``
    was hardened (plan P0.3 / finding B3). The credential is attached only when the URL really is
    the configured control plane, so a mislabelled call can never carry it to another host, and a
    caller that set its own ``Authorization`` keeps it.
    """
    from examlops.service_auth import control_plane_bearer  # noqa: PLC0415

    token = control_plane_bearer(config.CONTROL_PLANE_TOKEN)  # the token file first (ADR 0125)
    if service != "control_plane" or not token:
        return kwargs
    base = config.CONTROL_PLANE_URL.rstrip("/")
    if not (url == base or url.startswith(base + "/")):
        return kwargs
    headers = dict(kwargs.get("headers") or {})
    if not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = f"Bearer {token}"
    return {**kwargs, "headers": headers}


def _with_platform_service_auth(service: str, url: str, kwargs: dict) -> dict:
    """MLflow's / Prefect's credential when those servers require one (plan P3.6).

    Same rule as the control-plane bearer above: only for a URL that really is the configured
    MLflow or Prefect, and never over an ``Authorization`` the caller set itself.
    """
    if service not in ("mlflow", "prefect"):
        return kwargs
    try:
        from examlops.service_auth import headers_for
    except ImportError:
        return kwargs
    extra = headers_for(
        url,
        mlflow_base=config.MLFLOW_URL,
        prefect_base=getattr(config, "PREFECT_URL", None) or None,
    )
    if not extra:
        return kwargs
    headers = dict(kwargs.get("headers") or {})
    if not any(k.lower() == "authorization" for k in headers):
        headers.update(extra)
    return {**kwargs, "headers": headers}


def serving_admin_headers() -> dict[str, str]:
    """Headers for Ray Serve's admin routes (reload, traffic-rule push); empty when unset."""
    token = config.RAY_SERVE_ADMIN_TOKEN
    return {"Authorization": f"Bearer {token}"} if token else {}


def request_json(service: str, method: str, url: str, *, retries: int = 2, **kwargs):
    """Return (data, None) on success or (None, error_string) on failure.

    Retries up to `retries` times on transient network errors (RequestError)
    with exponential back-off. HTTP errors (4xx/5xx) are returned immediately
    without retrying since they represent a definitive server response.
    Calls to the control plane carry its bearer credential (``_with_control_plane_auth``).
    """
    kwargs = _with_control_plane_auth(service, url, kwargs)
    kwargs = _with_platform_service_auth(service, url, kwargs)
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
    return None, _format_error(service, url, last_exc or RuntimeError("unknown error"))


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
            resp = client.post(f"{self._base}/api/auth/login", json={"password": self._password})
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


def submit_retrain(body: dict, *, automated: bool = False) -> tuple[dict | None, str | None]:
    """Submit a retrain through the control plane's command API and wait for the dispatch.

    ``POST /v1/retrain`` accepts the retrain durably and a control-plane worker dispatches it
    (plan P1.6c; the synchronous ``POST /retrain`` is deprecated). The wait loop is
    ``examlops.retrain_command.follow`` — the one the CLI and MCP tools use — so the answer has
    the same shape: ``flow_run_id`` once dispatched, else ``command_id`` + ``state``. Without the
    ``examlops`` package the 202 answer itself is returned: the retrain is accepted either way.
    One ``Idempotency-Key`` per call, so ``request_json``'s transport retries resolve to the same
    command instead of submitting a second retrain.
    """
    import uuid  # noqa: PLC0415

    view, err = request_json(
        "control_plane",
        "POST",
        f"{config.CONTROL_PLANE_URL}/v1/retrain",
        json=body,
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    if err:
        return None, err
    try:
        from examlops import retrain_command  # noqa: PLC0415
    except ImportError:
        return view, None

    def fetch(command_id: str) -> dict:
        data, fetch_err = request_json(
            "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/commands/{command_id}"
        )
        if fetch_err:
            raise _FollowLost(fetch_err)
        return data

    wait = retrain_command.AUTOMATION_WAIT_SECONDS if automated else None
    try:
        return retrain_command.follow(view, fetch, wait=wait), None
    except _FollowLost:
        # Accepted, then the control plane stopped answering: the command is durable, so report
        # it as accepted rather than as a failure the operator might "fix" by submitting again.
        return retrain_command.outcome(view), None
    except retrain_command.RetrainNotDispatched as exc:
        return None, f"Error: {exc}"


class _FollowLost(Exception):
    """Following a submitted command failed; the command itself stands."""


_DASHBOARD: DashboardClient | None = None


def dashboard() -> DashboardClient:
    """Lazy singleton used by services.py and pipelines.py."""
    global _DASHBOARD
    if _DASHBOARD is None:
        _DASHBOARD = DashboardClient()
    return _DASHBOARD
