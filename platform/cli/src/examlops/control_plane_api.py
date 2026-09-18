"""The control plane's /v1 API as Python functions — GENERATED, do not edit.

Regenerate with ``make openapi-export`` (or ``python platform/ci/gen_cp_client.py``) after changing a
route; ``tests/unit/test_control_plane_client_generated.py`` fails while this file is stale.

Every function takes ``base`` (the control plane's URL) and ``token`` (a bearer credential) as
keyword arguments, defaulting to the CLI's configuration (``control_plane_url`` /
``control_plane_token``). Calls go through ``examlops.cli._client``, so errors surface as its
``ClientError`` with the server's ``detail`` — for /v1, the RFC 9457 problem document's.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from examlops.cli import _client


def _base_and_token(base: str | None, token: str | None) -> tuple[str, str]:
    if base is None or token is None:
        from examlops.cli._config import load_config

        cfg = load_config()
        base = cfg.control_plane_url if base is None else base
        token = (cfg.control_plane_token or "") if token is None else token
    return base.rstrip("/"), token


def _query(params: dict[str, Any]) -> str:
    present = {k: v for k, v in params.items() if v is not None}
    return f"?{urlencode(present)}" if present else ""


def _seg(value: Any) -> str:
    return quote(str(value), safe="")


__all__ = [
    "approve",
    "cancel_command",
    "get_command",
    "list_approvals",
    "list_commands",
    "list_models",
    "model_image",
    "model_meta",
    "model_readme",
    "modelzoo_config",
    "modelzoo_events",
    "modelzoo_status",
    "modelzoo_sync",
    "reject",
    "reload_registry",
    "report_changes",
    "retract_approval",
    "run_status",
    "set_modelzoo_config",
    "status",
    "submit_retrain",
]


def reload_registry(
    *, timeout: float | None = None, base: str | None = None, token: str | None = None
) -> Any:
    """``POST /v1/admin/reload``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/admin/reload"
    if timeout is None:
        return _client.post(url, {}, token=token)
    return _client.post(url, {}, token=token, timeout=timeout)


def list_approvals(*, status: Any = None, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/approvals``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/approvals" + _query({"status": status})
    return _client.get(url, token=token)


def retract_approval(approval_id: str, *, base: str | None = None, token: str | None = None) -> Any:
    """``DELETE /v1/approvals/{approval_id}``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/approvals/{_seg(approval_id)}"
    return _client.delete(url, token=token)


def approve(
    model_id: str,
    *,
    timeout: float | None = None,
    base: str | None = None,
    token: str | None = None,
) -> Any:
    """``POST /v1/approvals/{model_id}/approve``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/approvals/{_seg(model_id)}/approve"
    if timeout is None:
        return _client.post(url, {}, token=token)
    return _client.post(url, {}, token=token, timeout=timeout)


def reject(
    model_id: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float | None = None,
    base: str | None = None,
    token: str | None = None,
) -> Any:
    """``POST /v1/approvals/{model_id}/reject``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/approvals/{_seg(model_id)}/reject"
    if timeout is None:
        return _client.post(url, body or {}, token=token)
    return _client.post(url, body or {}, token=token, timeout=timeout)


def report_changes(
    *,
    body: dict[str, Any] | None = None,
    timeout: float | None = None,
    base: str | None = None,
    token: str | None = None,
) -> Any:
    """``POST /v1/changes``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/changes"
    if timeout is None:
        return _client.post(url, body or {}, token=token)
    return _client.post(url, body or {}, token=token, timeout=timeout)


def list_commands(
    *,
    cursor: Any = None,
    kind: Any = None,
    limit: Any = None,
    state: Any = None,
    base: str | None = None,
    token: str | None = None,
) -> Any:
    """``GET /v1/commands``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/commands" + _query(
        {"cursor": cursor, "kind": kind, "limit": limit, "state": state}
    )
    return _client.get(url, token=token)


def cancel_command(command_id: str, *, base: str | None = None, token: str | None = None) -> Any:
    """``DELETE /v1/commands/{command_id}``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/commands/{_seg(command_id)}"
    return _client.delete(url, token=token)


def get_command(command_id: str, *, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/commands/{command_id}``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/commands/{_seg(command_id)}"
    return _client.get(url, token=token)


def list_models(*, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/models``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/models"
    return _client.get(url, token=token)


def model_image(
    name: str, filename: str, *, base: str | None = None, token: str | None = None
) -> Any:
    """``GET /v1/models/{name}/images/{filename}``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/models/{_seg(name)}/images/{_seg(filename)}"
    return _client.get(url, token=token)


def model_meta(name: str, *, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/models/{name}/meta``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/models/{_seg(name)}/meta"
    return _client.get(url, token=token)


def model_readme(name: str, *, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/models/{name}/readme``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/models/{_seg(name)}/readme"
    return _client.get(url, token=token)


def modelzoo_config(*, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/modelzoo/config``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/modelzoo/config"
    return _client.get(url, token=token)


def set_modelzoo_config(
    *, body: dict[str, Any] | None = None, base: str | None = None, token: str | None = None
) -> Any:
    """``PUT /v1/modelzoo/config``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/modelzoo/config"
    return _client.put(url, body or {}, token=token)


def modelzoo_events(*, limit: Any = None, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/modelzoo/events``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/modelzoo/events" + _query({"limit": limit})
    return _client.get(url, token=token)


def modelzoo_status(*, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/modelzoo/status``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/modelzoo/status"
    return _client.get(url, token=token)


def modelzoo_sync(
    *, timeout: float | None = None, base: str | None = None, token: str | None = None
) -> Any:
    """``POST /v1/modelzoo/sync``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/modelzoo/sync"
    if timeout is None:
        return _client.post(url, {}, token=token)
    return _client.post(url, {}, token=token, timeout=timeout)


def submit_retrain(
    *,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    timeout: float | None = None,
    base: str | None = None,
    token: str | None = None,
) -> Any:
    """``POST /v1/retrain``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/retrain"
    if timeout is None:
        return _client.post(url, body or {}, token=token, idempotency_key=idempotency_key)
    return _client.post(
        url, body or {}, token=token, timeout=timeout, idempotency_key=idempotency_key
    )


def run_status(flow_run_id: str, *, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/runs/{flow_run_id}``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/runs/{_seg(flow_run_id)}"
    return _client.get(url, token=token)


def status(*, base: str | None = None, token: str | None = None) -> Any:
    """``GET /v1/status``."""
    base, token = _base_and_token(base, token)
    url = f"{base}/v1/status"
    return _client.get(url, token=token)
