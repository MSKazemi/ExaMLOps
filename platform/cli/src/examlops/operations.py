"""Operation handles for long-running work (ADR 0147 decision 5).

A retrain, a pipeline run or an HPC job does not finish inside the call that starts it. The call
returns an **operation id** and this module lets any caller — the ``exa ops`` CLI, an MCP tool, an
agent — read that operation's state, wait for it (bounded) and cancel it (only when the record
says that is possible).

There is no second store. An operation *is* a row of the control plane's ``control_plane_commands``
table, reached through its ``/v1/commands`` API (:mod:`examlops.control_plane_api`), so tenancy,
RBAC and the audit chain stay exactly where they already are, and the operation id is the command
id the submitting call already returned (``command_id``; also surfaced as ``operation_id``).

State vocabulary (MCP Tasks): ``working | input_required | completed | failed | cancelled``.
The control plane's own states are richer and are kept alongside as ``raw_state``:

============================  =================================================================
control plane                 operation state
============================  =================================================================
``pending``                   ``working`` (queued, cancellable)
``dispatching``               ``working`` (being handed to the scheduler, not cancellable)
``failed``                    ``working`` — *not* a failure: the worker retries with backoff
                              until ``dead`` (cancellable while it waits)
``dead``                      ``failed`` (retries exhausted)
``cancelled``                 ``cancelled``
``succeeded`` + flow run      follows the flow run: ``COMPLETED`` → ``completed``;
                              ``FAILED``/``CRASHED``/``MISSING`` → ``failed``; ``CANCELLED`` →
                              ``cancelled``; anything else (or not yet reconciled) → ``working``
``succeeded`` (no flow run)   ``completed``
============================  =================================================================

No control-plane state maps to ``input_required`` today (an approval waits in the approvals
queue, not in the command record); the value is part of the vocabulary so callers can handle it
when a future kind needs it. A state outside the table above is reported as ``unknown`` with its
``raw_state`` — never guessed into the vocabulary.

Honesty rules, each pinned by ``tests/unit/test_operations.py``:

* :func:`wait` polls until a terminal state or the timeout and then *returns*; it never blocks
  forever, and a timeout is reported as ``timed_out: true`` with the last observed state.
* :func:`cancel` is asked of the control plane only for a state the record says is cancellable
  (``pending`` / ``failed``, i.e. not yet dispatched). Anything else is refused up front with
  ``not_cancellable``. ``cancelled: true`` is reported only when the record read back says
  ``cancelled`` — a request is never presented as an outcome.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from examlops.cli._client import ClientError

__all__ = [
    "CANCELLABLE_RAW",
    "STATES",
    "TERMINAL",
    "cancel",
    "list_operations",
    "normalize",
    "status",
    "wait",
]

STATES = ("working", "input_required", "completed", "failed", "cancelled")
TERMINAL = frozenset({"completed", "failed", "cancelled"})
#: Control-plane states the DELETE route accepts (``cancel_command_v1``): not yet dispatched.
CANCELLABLE_RAW = frozenset({"pending", "failed"})

DEFAULT_WAIT_TIMEOUT_S = 300.0
DEFAULT_WAIT_INTERVAL_S = 2.0
MAX_WAIT_TIMEOUT_S = 3600.0
_LIST_PAGE = 200
_LIST_MAX_PAGES = 5


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def default_timeout() -> float:
    """``EXAMLOPS_OPS_WAIT_TIMEOUT`` seconds (default 300), never above one hour."""
    return min(_env_float("EXAMLOPS_OPS_WAIT_TIMEOUT", DEFAULT_WAIT_TIMEOUT_S), MAX_WAIT_TIMEOUT_S)


def default_interval() -> float:
    """``EXAMLOPS_OPS_WAIT_INTERVAL`` seconds between polls (default 2)."""
    return _env_float("EXAMLOPS_OPS_WAIT_INTERVAL", DEFAULT_WAIT_INTERVAL_S)


def normalize(raw_state: str | None, run_state: str | None = None, has_run: bool = False) -> str:
    """Map a control-plane command state (+ its flow run's state) into the operation vocabulary."""
    raw = (raw_state or "").lower()
    if raw in ("pending", "dispatching", "failed"):
        return "working"
    if raw == "dead":
        return "failed"
    if raw == "cancelled":
        return "cancelled"
    if raw == "succeeded":
        if not has_run:
            return "completed"
        run = (run_state or "").upper()
        if run == "COMPLETED":
            return "completed"
        if run in ("FAILED", "CRASHED", "MISSING"):
            return "failed"
        if run == "CANCELLED":
            return "cancelled"
        return "working"
    return "unknown"


def _shape(view: dict[str, Any]) -> dict[str, Any]:
    """One command view → the operation document callers see."""
    raw = str(view.get("state") or "")
    result = view.get("result") or {}
    flow_run = result.get("flow_run_id")
    state = normalize(raw, view.get("run_state"), bool(flow_run))
    detail = ""
    if raw == "failed":
        detail = "dispatch failed; the control plane is retrying"
    elif raw == "succeeded" and flow_run and state == "working":
        detail = "dispatched; the flow run has not finished"
    doc: dict[str, Any] = {
        "operation_id": view.get("command_id"),
        "kind": view.get("kind"),
        "state": state,
        "terminal": state in TERMINAL,
        "cancellable": raw in CANCELLABLE_RAW,
        "raw_state": raw,
        "run_state": view.get("run_state"),
        "attempts": view.get("attempts"),
        "flow_run_id": flow_run,
        "last_error": view.get("last_error"),
        "created_at": view.get("created_at"),
        "updated_at": view.get("updated_at"),
    }
    if detail:
        doc["detail"] = detail
    return doc


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message, **extra}


def _client_error(exc: ClientError, operation_id: str = "") -> dict[str, Any]:
    status_code = getattr(exc, "status", None)
    if status_code == 404:
        return _err("not_found", f"no operation {operation_id!r} in your tenant", status=404)
    return _err("control_plane_error", str(exc), status=status_code)


def _api() -> Any:
    from examlops import control_plane_api

    return control_plane_api


#: Offline-inference jobs (ADR 0149) are operations too, but they run inline and have no control-
#: plane command record: their id (``off-…``) is read from the local job store instead.
OFFLINE_PREFIX = "off-"


def _offline() -> Any:
    from examlops import offline

    return offline


def status(
    operation_id: str, *, base: str | None = None, token: str | None = None
) -> dict[str, Any]:
    """Read one operation: ``{"ok": True, "operation": {...}}`` or a structured error."""
    if not operation_id or not operation_id.strip():
        return _err("invalid_id", "operation_id must not be empty")
    if operation_id.startswith(OFFLINE_PREFIX):
        return _offline().operation_view(operation_id)
    try:
        view = _api().get_command(operation_id, base=base, token=token)
    except ClientError as exc:
        return _client_error(exc, operation_id)
    except Exception as exc:  # noqa: BLE001 - an unreachable control plane is a result, not a crash
        return _err("control_plane_unreachable", str(exc))
    return {"ok": True, "operation": _shape(dict(view))}


def list_operations(
    *,
    state: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    base: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Newest-first operations of the caller's tenant, optionally by operation ``state``.

    ``state`` is in the operation vocabulary and is applied here (the control plane filters only by
    its own raw states), scanning up to five pages of 200 for at most ``limit`` matches.
    """
    if state is not None and state not in (*STATES, "unknown"):
        return _err("invalid_state", f"state must be one of {', '.join(STATES)}")
    limit = max(1, min(int(limit), 200))
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = False
    try:
        for _ in range(_LIST_MAX_PAGES):
            page = _api().list_commands(
                cursor=cursor, kind=kind, limit=_LIST_PAGE, base=base, token=token
            )
            for view in page.get("items", []):
                doc = _shape(dict(view))
                if state is None or doc["state"] == state:
                    found.append(doc)
                    if len(found) >= limit:
                        return {"ok": True, "operations": found, "truncated": False}
            cursor = page.get("next_cursor")
            if not cursor:
                break
        else:
            truncated = True
    except ClientError as exc:
        return _client_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _err("control_plane_unreachable", str(exc))
    return {"ok": True, "operations": found, "truncated": truncated}


def wait(
    operation_id: str,
    *,
    timeout: float | None = None,
    interval: float | None = None,
    base: str | None = None,
    token: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll until the operation is terminal or ``timeout`` seconds pass; always returns.

    ``timeout`` defaults to :func:`default_timeout`, is capped at an hour, and ``0`` means "look
    once". The result carries ``timed_out`` — a timeout is not a failure of the operation, which
    keeps running; call again to keep waiting.
    """
    if timeout is not None and timeout < 0:
        return _err("invalid_timeout", "timeout must be >= 0")
    budget = min(default_timeout() if timeout is None else float(timeout), MAX_WAIT_TIMEOUT_S)
    step = default_interval() if interval is None else max(0.05, float(interval))
    deadline = clock() + budget
    while True:
        out = status(operation_id, base=base, token=token)
        if not out.get("ok"):
            return out
        op = out["operation"]
        if op["terminal"]:
            return {"ok": True, "operation": op, "timed_out": False}
        remaining = deadline - clock()
        if remaining <= 0:
            return {"ok": True, "operation": op, "timed_out": True}
        sleep(min(step, remaining))


def cancel(
    operation_id: str, *, base: str | None = None, token: str | None = None
) -> dict[str, Any]:
    """Ask the control plane to cancel an operation that has not been dispatched.

    Reads the record first; a terminal or already-dispatched operation is refused with
    ``not_cancellable`` and the control plane is not asked. ``cancelled`` in the result is true
    only if the record returned by the control plane says ``cancelled``.
    """
    if operation_id.startswith(OFFLINE_PREFIX):
        out = _offline().cancel(operation_id)
        if out.get("job") is not None:
            out["operation"] = _offline().operation_view(operation_id).get("operation")
        return out
    before = status(operation_id, base=base, token=token)
    if not before.get("ok"):
        return before
    op = before["operation"]
    if not op["cancellable"]:
        why = (
            f"operation is already {op['state']}"
            if op["terminal"]
            else "it has been dispatched; only queued operations can be cancelled here — "
            "stop the underlying run in its scheduler"
        )
        return _err(
            "not_cancellable",
            f"operation {operation_id} ({op['raw_state']}) cannot be cancelled: {why}",
            operation=op,
            cancelled=False,
        )
    try:
        view = _api().cancel_command(operation_id, base=base, token=token)
    except ClientError as exc:
        # 409 = it moved on between our read and the request (dispatched, or already cancelled).
        out = _client_error(exc, operation_id)
        if out.get("status") == 409:
            out["code"] = "not_cancellable"
        after = status(operation_id, base=base, token=token)
        if after.get("ok"):
            out["operation"] = after["operation"]
        out["cancelled"] = bool(after.get("ok") and after["operation"]["state"] == "cancelled")
        return out
    except Exception as exc:  # noqa: BLE001
        return {**_err("control_plane_unreachable", str(exc)), "cancelled": False}
    doc = _shape(dict(view))
    return {"ok": True, "operation": doc, "cancelled": doc["state"] == "cancelled"}
