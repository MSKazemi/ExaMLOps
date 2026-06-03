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


def _send(req: urllib.request.Request, url: str) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise ClientError(f"HTTP {exc.code} from {url}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"Service unreachable at {url}: {exc.reason}") from exc
