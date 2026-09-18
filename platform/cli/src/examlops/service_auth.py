"""Credentials for the platform's own MLflow and Prefect, for callers that speak raw HTTP (P3.6).

Both servers can require authentication — MLflow's ``basic-auth`` app, Prefect's API auth string —
and their Python SDKs pick the credential up from the environment on their own
(``MLFLOW_TRACKING_USERNAME``/``PASSWORD`` or ``MLFLOW_TRACKING_TOKEN``; ``PREFECT_API_AUTH_STRING``
or ``PREFECT_API_KEY``). Everything that calls them over plain HTTP — the control plane's Prefect
gateway and snapshot compiler, the dashboard, the agent, the CLI — asks this module for the same
headers, read from the same variables, so turning authentication on is configuration only.

With nothing set, every function returns no header: authentication is opt-in and an unsecured
stack behaves exactly as before.
"""

from __future__ import annotations

import base64
import os


def _basic(user_colon_password: str) -> dict[str, str]:
    token = base64.b64encode(user_colon_password.encode()).decode()
    return {"Authorization": f"Basic {token}"}


def mlflow_headers() -> dict[str, str]:
    """The Authorization header MLflow's own client would send, or ``{}``."""
    token = os.getenv("MLFLOW_TRACKING_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    user = os.getenv("MLFLOW_TRACKING_USERNAME", "").strip()
    password = os.getenv("MLFLOW_TRACKING_PASSWORD", "")
    return _basic(f"{user}:{password}") if user and password else {}


def prefect_headers() -> dict[str, str]:
    """The Authorization header Prefect's own client would send, or ``{}``.

    ``PREFECT_API_AUTH_STRING`` is the ``user:password`` a self-hosted server was started with;
    ``PREFECT_API_KEY`` is a Prefect Cloud key. An empty value means "no authentication" — on the
    client side; see the compose notes for why the *server* variable must never be set empty.
    """
    auth_string = os.getenv("PREFECT_API_AUTH_STRING", "")
    if auth_string:
        return _basic(auth_string)
    key = os.getenv("PREFECT_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _base(url: str) -> str:
    return url.rstrip("/").lower()


def _under(target: str, base: str) -> bool:
    return bool(base) and (target == base or target.startswith(base + "/"))


def headers_for(
    url: str,
    *,
    mlflow_base: str | None = None,
    prefect_base: str | None = None,
    serving_base: str | None = None,
    serving_token: str = "",
) -> dict[str, str]:
    """Credentials for ``url`` when it points at the platform's MLflow, Prefect or model serving.

    For generic clients (the CLI's) that do not know which service a URL belongs to. The bases
    default to ``MLFLOW_TRACKING_URI`` and ``PREFECT_API_URL``; a caller passes its own configured
    values when it has them. The serving credential (a serving-gateway virtual key, or an access
    token from the data center's identity provider) goes only to URLs under ``serving_base``, the
    configured ``ray_serve_url``: a key for inference must never reach another service.
    """
    target = _base(url)
    mlflow = _base(mlflow_base or os.getenv("MLFLOW_TRACKING_URI") or "")
    prefect = _base(prefect_base or os.getenv("PREFECT_API_URL") or "")
    if _under(target, mlflow):
        return mlflow_headers()
    if _under(target, prefect):
        return prefect_headers()
    token = serving_token.strip()
    if token and _under(target, _base(serving_base or "")):
        return {"Authorization": f"Bearer {token}"}
    return {}


# ── the control plane's bearer, from a file that something else keeps fresh (ADR 0125) ────────


def token_from_file(path: str) -> str:
    """The bearer credential in ``path``, read afresh on every call; ``""`` if unreadable.

    For credentials another process rotates: a JWT-SVID that SPIRE's ``spiffe-helper`` rewrites
    before it expires (every few minutes), or a Kubernetes Secret the kubelet updates in place.
    Deliberately not cached on the file's mtime and size: two tokens of the same length written
    within one timestamp tick would look unchanged, and a few hundred bytes cost microseconds.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def control_plane_bearer(static: str | None = None) -> str:
    """What a platform service sends the control plane as its bearer credential.

    ``CONTROL_PLANE_TOKEN_FILE`` first, read on every call, so a short-lived workload identity is
    never sent after it expired; then ``static`` (default: ``CONTROL_PLANE_TOKEN``). A configured
    file that is missing or empty falls back to the static credential, so a service can be moved to
    workload identity before its first identity has been written.
    """
    path = os.getenv("CONTROL_PLANE_TOKEN_FILE", "").strip()
    if path:
        token = token_from_file(path)
        if token:
            return token
    return static if static is not None else os.getenv("CONTROL_PLANE_TOKEN", "")
