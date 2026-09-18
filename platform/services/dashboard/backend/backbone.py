"""Platform events from the NATS backbone onto the dashboard's live stream (ADR 0124, plan P2.3).

The realtime bus (``realtime.py``) only carried what *this* dashboard process did — an approval
clicked here reached browsers connected here, and nothing the CLI, the control plane, the agent or
another dashboard replica did reached anyone. With the event backbone on NATS, every dashboard
replica watches the platform's event stream and republishes each event onto its bus, so
``GET /api/v1/stream`` carries the platform's events whichever surface produced them.

* **Broadcast, not a work queue.** Each replica gets every new event (an ephemeral ordered
  consumer — nothing durable is left behind when a replica goes away). A live view has no effect to
  deduplicate, so there is no inbox here.
* **Thread hand-off.** The NATS callback runs on the connection's thread; the bus's queues belong
  to the dashboard's event loop and are not thread-safe, so every event crosses with
  ``call_soon_threadsafe``.
* **Tenancy.** An event of tenant ``default`` is platform-wide and goes to every subscriber; an
  event of another tenant only to that tenant's subscribers (the bus's R4 filter).

Off unless ``EXAMLOPS_EVENT_PUBLISHER=nats``; ``DASHBOARD_BACKBONE=off`` disables it explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from realtime import bus

logger = logging.getLogger(__name__)

# Backbone topic family → the dashboard's channel namespaces (realtime.CHANNELS).
_NAMESPACE = {
    "retrain": "job",
    "modelzoo": "job",
    "approval": "approval",
    "alert": "alert",
    "serving": "deploy",
    "model": "deploy",
}

_state: dict[str, Any] = {"stop": None, "relayed": 0, "error": None}


def channel_for(topic: str) -> str:
    """``retrain.run_failed`` → ``job.retrain_run_failed``; unknown families → ``event.<topic>``.

    The family is kept in the event name, so ``deploy.*`` subscribers can tell a traffic change
    (``deploy.serving_traffic_changed``) from an alias move (``deploy.model_alias_changed``).
    """
    family, _, rest = topic.partition(".")
    namespace = _NAMESPACE.get(family, "event")
    name = topic.replace(".", "_") if namespace not in ("approval", "alert") else rest or family
    return f"{namespace}.{name}"


def frame_for(event: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """The ``(channel, data, tenant)`` a CloudEvent becomes on the dashboard bus."""
    from examlops.events.envelope import topic_of

    tenant = event.get("examlopstenant")
    data = dict(event.get("data") or {})
    data["_event"] = {
        "id": event.get("id"),
        "type": event.get("type"),
        "time": event.get("time"),
        "actor": event.get("examlopsactor"),
    }
    return channel_for(topic_of(event)), data, (None if tenant in (None, "default") else tenant)


def _enabled() -> bool:
    if os.getenv("DASHBOARD_BACKBONE", "auto").strip().lower() == "off":
        return False
    return os.getenv("EXAMLOPS_EVENT_PUBLISHER", "log").strip().lower() == "nats"


def _relay(loop: asyncio.AbstractEventLoop, body: bytes) -> None:
    """NATS thread → the dashboard's loop."""
    from examlops.events.envelope import decode

    try:
        channel, data, tenant = frame_for(decode(body))
    except Exception as exc:  # noqa: BLE001 - a malformed message is dropped, not fatal
        logger.debug("dropping an undecodable backbone message: %s", exc)
        return
    _state["relayed"] += 1
    loop.call_soon_threadsafe(lambda: bus.publish(channel, data, tenant=tenant))


def start(
    loop: asyncio.AbstractEventLoop, *, watch: Callable[..., Callable[[], None]] | None = None
) -> bool:
    """Begin relaying; returns whether the bridge is running. Never raises."""
    if not _enabled():
        return False
    try:
        if watch is None:
            from examlops.events import nats_backend

            watch = nats_backend.shared().watch
        _state["stop"] = watch(None, lambda body: _relay(loop, body))
        _state["error"] = None
        logger.info("Dashboard live stream is relaying platform events from the NATS backbone")
        return True
    except Exception as exc:  # noqa: BLE001 - the dashboard runs fine without the bridge
        _state["error"] = str(exc)
        logger.warning("Backbone bridge not started (live stream shows local events only): %s", exc)
        return False


def stop() -> None:
    stopper, _state["stop"] = _state["stop"], None
    if stopper is not None:
        stopper()


def status() -> dict[str, Any]:
    return {
        "enabled": _enabled(),
        "running": _state["stop"] is not None,
        "relayed": _state["relayed"],
        "error": _state["error"],
    }
