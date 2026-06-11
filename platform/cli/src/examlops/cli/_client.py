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


def post(url: str, body: dict[str, Any], token: str = "") -> Any:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return _send(req, url)


def delete(url: str, token: str | None = None) -> Any:
    """Send DELETE request, return parsed JSON response."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="DELETE", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ClientError(f"HTTP {e.code} {e.reason}: {body}") from e
    except urllib.error.URLError as e:
        raise ClientError(str(e.reason)) from e


def put(url: str, body: Any, token: str | None = None) -> Any:
    """Send PUT request with JSON body, return parsed JSON response."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body_resp = resp.read()
            return json.loads(body_resp) if body_resp else {}
    except urllib.error.HTTPError as e:
        body_resp = e.read().decode("utf-8", errors="replace")
        raise ClientError(f"HTTP {e.code} {e.reason}: {body_resp}") from e
    except urllib.error.URLError as e:
        raise ClientError(str(e.reason)) from e


def _send(req: urllib.request.Request, url: str) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise ClientError(f"HTTP {exc.code} from {url}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"Service unreachable at {url}: {exc.reason}") from exc
