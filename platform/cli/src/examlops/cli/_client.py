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


def post(
    url: str,
    body: dict[str, Any],
    token: str = "",
    timeout: float = 10.0,
    idempotency_key: str | None = None,
) -> Any:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key:
        # Makes a repeated submission (a retry after a timeout, a re-run of the same automation
        # cycle) resolve to the one command the first attempt created (plan P1.7).
        headers["Idempotency-Key"] = idempotency_key
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


def _platform_service_auth(req: urllib.request.Request, url: str) -> None:
    """MLflow's / Prefect's credential for a call to the configured MLflow or Prefect (P3.6), and
    the serving credential (``serving_token``) for a call to the configured ``ray_serve`` URL.

    Only when the URL is under one of those configured bases, and never over an
    ``Authorization`` the caller already set (a control-plane bearer, or the serving admin token).
    """
    if req.has_header("Authorization"):
        return
    try:
        from examlops.cli._config import load_config
        from examlops.service_auth import headers_for

        cfg = load_config()
        extra = headers_for(
            url,
            mlflow_base=cfg.mlflow_url,
            prefect_base=cfg.prefect_url,
            serving_base=cfg.ray_serve_url,
            serving_token=cfg.serving_token,
        )
    except Exception:  # noqa: BLE001 - a config problem must not break an unauthenticated call
        return
    for key, value in extra.items():
        req.add_header(key, value)


def _send(req: urllib.request.Request, url: str, timeout: float = 10.0) -> Any:
    _platform_service_auth(req, url)
    from examlops.policy import http_gate

    if http_gate.approval_ack_active():  # a human already answered this command's policy prompt
        req.add_header(http_gate.APPROVAL_HEADER, "true")
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


#: Which credential a 401 is about, per service: the ``Config`` field holding that service's base
#: URL, the environment variable, the ``exa config set`` key, and what to call the service.
#:
#: This helper serves every command, and the 401 message used to name Skipper's ``AGENT_API_KEY``
#: whatever had answered — so ``exa dataplane pull --remote``, which needs
#: ``EXAMLOPS_DATAPLANE_TOKEN``, sent operators to the wrong variable (live pass 2). Ray Serve is
#: deliberately absent: it has two credentials (``serving_token`` and ``ray_serve_admin_token``)
#: and which one a route wants cannot be told from the URL, and naming the wrong one is the bug
#: this table exists to fix.
_SERVICE_TOKENS: tuple[tuple[str, str, str, str], ...] = (
    ("dataplane_url", "EXAMLOPS_DATAPLANE_TOKEN", "dataplane_token", "the dataplane service"),
    ("control_plane_url", "CONTROL_PLANE_TOKEN", "control_plane_token", "the control plane"),
    ("agent_url", "AGENT_API_KEY", "agent_token", "Skipper"),
    ("dashboard_url", "DASHBOARD_TOKEN", "dashboard_token", "the dashboard"),
)


def _auth_hint(url: str) -> str:
    """How to authenticate to whichever configured service ``url`` belongs to, or ``""``.

    Matched against the configured base URLs rather than guessed from the path, so a site that
    moved a service still gets the right variable named.
    """
    try:
        from examlops.cli._config import load_config

        cfg = load_config()
    except Exception:  # noqa: BLE001 - a config problem must not replace the auth error
        return ""
    target = url.rstrip("/")
    for field, env, key, label in _SERVICE_TOKENS:
        base = str(getattr(cfg, field, "") or "").rstrip("/")
        if base and (target == base or target.startswith(base + "/")):
            return f" Set {env}, or run `exa config set {key}` ({label})."
    return ""


def _raise_http(exc: urllib.error.HTTPError, url: str) -> None:
    code = exc.code
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""

    if code == 401:
        hint = _auth_hint(url) or " Configure the token that service requires."
        raise ClientError(f"Authentication required ({url}).{hint}", status=code) from exc
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
