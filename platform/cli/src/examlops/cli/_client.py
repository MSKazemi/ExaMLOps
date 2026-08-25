from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
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


def post_sse(
    url: str, body: dict[str, Any], token: str = "", timeout: float = 120.0
) -> Iterator[dict[str, Any]]:
    """POST and yield each ``data:`` frame of a Server-Sent Events response, decoded.

    The plain ``post`` above waits for the whole body, so a slow answer is indistinguishable
    from a hang. This yields as the server writes.

    Note what ``timeout`` means here, because it is *not* what it means for ``post``. urllib
    applies it to each socket operation, so on a streamed response it is an **idle** timeout —
    the gap allowed between two frames — not a budget for the whole answer. That is the useful
    semantic for a chat stream: a long answer is fine, a silent one is not.

    Frames that are not JSON are skipped rather than raising: the wire is a text protocol and a
    stray comment or keep-alive must not abort a stream that is otherwise fine. The terminal
    ``[DONE]`` sentinel ends iteration.
    """
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    return
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame, dict):
                    yield frame
    except urllib.error.HTTPError as exc:
        _raise_http(exc, url)
    except urllib.error.URLError as exc:
        _raise_url(exc, url)
    except TimeoutError as exc:
        raise ClientError(f"Stream from {url} went silent for {timeout:g}s") from exc


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
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                # A 200 whose body is not JSON — an HTML error page from a proxy, a stray SSE
                # stream, a captive portal. Left unhandled this reached the user as a raw
                # traceback from inside json/decoder.py, which says nothing about what to do.
                snippet = " ".join(body.split())[:120]
                raise ClientError(
                    f"{url} returned a 200 that is not JSON: {snippet!r}. "
                    "Something other than the expected service is answering on that address."
                ) from exc
    except urllib.error.HTTPError as exc:
        _raise_http(exc, url)
    except urllib.error.URLError as exc:
        _raise_url(exc, url)
    except TimeoutError as exc:
        # A read-phase socket timeout is raised bare (not wrapped in URLError), so it would
        # otherwise leak past every caller. Wrap it so callers catching ClientError degrade
        # gracefully (e.g. MCP tools return an ok:False envelope instead of crashing).
        raise ClientError(f"Timed out after {timeout:g}s connecting to {url}") from exc


def _raise_http(exc: urllib.error.HTTPError, url: str) -> None:
    code = exc.code
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""

    if code == 401:
        raise ClientError(
            f"Authentication required ({url}). "
            "Configure the token required by that service (for Skipper: AGENT_API_KEY or "
            "`exa config set agent_token` and use the hidden prompt).",
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
        # The server answered, so it is running: prefer its own message over a generic one.
        detail = _extract_detail(body)
        message = (
            f"Server error {code} from {url}: {detail}"
            if detail
            else f"Server error {code} from {url}. Check service logs: exa stack logs"
        )
        raise ClientError(message, status=code) from exc
    # Everything not special-cased above — 400 most of all. The control plane answers an
    # unknown model or dataset with `HTTPException(400, "... Supported: [...]")`, and that list
    # is precisely what a caller needs in order to retry. Dropping it left an agent (and the
    # operator reading its answer) with a number instead of a reason, so the body wins here too;
    # the code stays in the message for anyone grepping for it.
    detail = _extract_detail(body)
    if detail:
        raise ClientError(f"HTTP {code} from {url}: {detail}", status=code) from exc
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
    """Best-effort human-readable error out of a JSON error body.

    Handles FastAPI's ``{"detail": …}``, a bare ``{"message": …}``, and the OpenAI-compatible
    ``{"error": {"message": …}}`` the agent bridge returns — without the last one, an upstream
    LLM failure reaches the operator as a bare "Server error 500" with the cause thrown away.
    """
    try:
        d = json.loads(body)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    err = d.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if isinstance(err, str) and err:
        return err
    return str(d.get("detail") or d.get("message") or "")
