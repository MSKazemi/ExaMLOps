"""Submit a retrain through the control plane's command API and wait for it to be dispatched.

The deprecated ``POST /retrain`` dispatches to Prefect inside the HTTP request: the caller waits on
Prefect, and a Prefect outage becomes the caller's error even though nothing was wrong with the
request. ``POST /v1/retrain`` accepts the retrain durably (202 + a command to follow) and a worker
dispatches it, retrying with backoff. Callers that want the old answer — a ``flow_run_id`` to
report — submit here, and this waits a bounded time for the dispatch:

* **dispatched in time** → the old response shape: ``flow_run_id``, ``deployment`` and
  ``status_url`` (now ``/v1/runs/<id>``), plus ``command_id`` and ``state``.
* **still queued when the wait ends** — admission full, Prefect briefly down — → ``state`` is
  ``pending`` or ``failed`` with no ``flow_run_id``, and ``status_url`` points at the command.
  The retrain is *not* lost: the control plane dispatches it when it can. Callers report
  "accepted, not yet dispatched" rather than an error.
* **given up on** (``dead``) or **cancelled** → :class:`RetrainNotDispatched`, a ``ClientError``,
  so every caller's existing error path still applies.

The wait loop is written once (:func:`follow` / :func:`follow_async`) against a ``fetch`` function,
so the CLI (through the generated ``control_plane_api``), the agent (through its own HTTP layer)
and the async bus bridge share it.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from examlops.cli._client import ClientError

#: Default time to wait for a submitted retrain to be dispatched.
DEFAULT_WAIT_SECONDS = float(os.getenv("EXAMLOPS_RETRAIN_WAIT_SECONDS", "") or 30.0)
#: What automated loops (drift trigger, autopilot) wait: the worker normally dispatches within
#: about a second, and a loop over many models must not stall on a Prefect outage — a retrain
#: still queued when this ends is dispatched later all the same.
AUTOMATION_WAIT_SECONDS = min(5.0, DEFAULT_WAIT_SECONDS)

_TERMINAL = frozenset({"succeeded", "dead", "cancelled"})
_POLL_START, _POLL_MAX = 0.25, 2.0


class RetrainNotDispatched(ClientError):
    """The control plane gave up on the retrain (``dead``) or it was cancelled."""

    def __init__(self, view: dict[str, Any]):
        state = view.get("state", "?")
        detail = view.get("last_error") or "no error recorded"
        super().__init__(f"retrain command {view.get('command_id')} {state}: {detail}")
        self.view = view


def outcome(view: dict[str, Any]) -> dict[str, Any]:
    """The caller-facing answer for a command view (see the module docstring)."""
    state = view.get("state")
    if state in ("dead", "cancelled"):
        raise RetrainNotDispatched(view)
    out: dict[str, Any] = {
        "command_id": view.get("command_id"),
        "state": state,
        "status_url": view.get("status_url"),
    }
    result = view.get("result") or {}
    out.update(result)
    if result.get("flow_run_id"):
        out["status_url"] = f"/v1/runs/{result['flow_run_id']}"
    if state != "succeeded" and view.get("last_error"):
        out["last_error"] = view["last_error"]
    return out


def dispatched(answer: dict[str, Any]) -> bool:
    """True when ``answer`` names the flow run the retrain became."""
    return bool(answer.get("flow_run_id"))


def _delays(wait: float) -> list[float]:
    delays, delay, total = [], _POLL_START, 0.0
    while total < wait:
        step = min(delay, wait - total)
        delays.append(step)
        total += step
        delay = min(delay * 2, _POLL_MAX)
    return delays


def follow(
    view: dict[str, Any],
    fetch: Callable[[str], dict[str, Any]],
    *,
    wait: float | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Poll ``fetch(command_id)`` until the command is terminal or ``wait`` seconds have passed."""
    sleep = sleep or time.sleep
    for delay in _delays(DEFAULT_WAIT_SECONDS if wait is None else wait):
        if view.get("state") in _TERMINAL:
            break
        sleep(delay)
        view = fetch(str(view["command_id"]))
    return outcome(view)


async def follow_async(
    view: dict[str, Any],
    fetch: Callable[[str], Awaitable[dict[str, Any]]],
    *,
    wait: float | None = None,
) -> dict[str, Any]:
    """:func:`follow` for an event loop: sleeps with ``asyncio.sleep``."""
    for delay in _delays(DEFAULT_WAIT_SECONDS if wait is None else wait):
        if view.get("state") in _TERMINAL:
            break
        await asyncio.sleep(delay)
        view = await fetch(str(view["command_id"]))
    return outcome(view)


def submit(
    body: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    wait: float | None = None,
    base: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """``POST /v1/retrain`` and wait for the dispatch. Errors surface as ``ClientError``."""
    from examlops import control_plane_api as cp  # noqa: PLC0415 - keeps import light

    view = cp.submit_retrain(body=body, idempotency_key=idempotency_key, base=base, token=token)
    return follow(view, lambda cid: cp.get_command(cid, base=base, token=token), wait=wait)
