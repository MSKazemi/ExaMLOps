from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ClientError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def get(url: str, token: str = "") -> Any:
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    return _send(req, url)


def post(url: str, body: dict[str, Any], token: str = "", timeout: float = 10.0) -> Any:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return _send(req, url, timeout=timeout)


def delete(url: str, token: str | None = None) -> Any:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="DELETE", headers=headers)
    return _send(req, url, timeout=30)


def put(url: str, body: Any, token: str | None = None) -> Any:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    return _send(req, url, timeout=30)


def _send(req: urllib.request.Request, url: str, timeout: float = 10.0) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        _raise_http(exc, url)
    except urllib.error.URLError as exc:
        _raise_url(exc, url)


def _raise_http(exc: urllib.error.HTTPError, url: str) -> None:
    code = exc.code
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""

    if code == 401:
        raise ClientError(
            f"Authentication required ({url}). "
            "Set CONTROL_PLANE_TOKEN or run: exa config set control_plane_token <token>",
            status=code,
        ) from exc
    if code == 403:
        raise ClientError(
            f"Access denied ({url}). Check your token has the required permissions.",
            status=code,
        ) from exc
    if code == 404:
        raise ClientError(f"Not found: {url}", status=code) from exc
    if code == 409:
        # Surface the API message directly — it's usually informative
        detail = _extract_detail(body) or "Conflict"
        raise ClientError(detail, status=code) from exc
    if code == 422:
        detail = _extract_detail(body) or body[:200]
        raise ClientError(f"Validation error: {detail}", status=code) from exc
    if code == 429:
        retry = exc.headers.get("Retry-After", "")
        retry_hint = f" — retry after {retry}s" if retry else ""
        raise ClientError(
            f"Rate limit reached{retry_hint}. Slow down or check RETRAIN_RATE_LIMIT_PER_MIN.",
            status=code,
        ) from exc
    if code >= 500:
        raise ClientError(
            f"Server error {code} from {url}. Check service logs: exa stack logs",
            status=code,
        ) from exc
    raise ClientError(f"HTTP {code} from {url}", status=code) from exc


def _raise_url(exc: urllib.error.URLError, url: str) -> None:
    reason = str(exc.reason)
    if "refused" in reason.lower() or "111" in reason:
        raise ClientError(
            f"Service unreachable at {url} (connection refused). "
            "Is the stack running? Try: exa stack status  or  exa stack up",
        ) from exc
    if "timed out" in reason.lower():
        raise ClientError(
            f"Request timed out ({url}). Service may be overloaded. Check: exa status",
        ) from exc
    raise ClientError(f"Network error connecting to {url}: {reason}") from exc


def _extract_detail(body: str) -> str:
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            return str(d.get("detail") or d.get("message") or "")
    except Exception:
        pass
    return ""
