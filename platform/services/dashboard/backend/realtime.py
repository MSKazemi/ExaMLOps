"""Realtime event gateway for the dashboard (F8 / ADR 0058, R3–R7).

An in-process pub/sub bus that fans typed events out to SSE subscribers. Backend components
``publish()`` events on dotted channels (``job.started``, ``drift.critical``, ``alert.firing``,
``deploy.done``, ``approval.pending``, ``event.*``); each subscriber receives only the channels
it subscribed to (glob patterns), filtered further by tenant (R4).

Backpressure (R7): every subscription has a bounded queue. Under an event flood the oldest queued
event is dropped to keep the stream fresh, and a per-subscription ``dropped`` counter is bumped so
the client can be told it missed events rather than silently diverging.
"""

from __future__ import annotations

import asyncio
import fnmatch
from dataclasses import dataclass, field

# Canonical channel namespaces (R3). Publishers should use "<namespace>.<event>".
CHANNELS = ("job", "drift", "alert", "deploy", "approval", "event")

DEFAULT_BUFFER = 100


@dataclass
class Event:
    channel: str
    data: dict
    tenant: str | None = None  # None = broadcast to all tenants


@dataclass(eq=False)  # identity-based hashing so instances can live in the bus's set
class Subscription:
    patterns: tuple[str, ...]
    tenant: str | None = None  # None = an operator that sees every tenant's events
    buffer: int = DEFAULT_BUFFER
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue())
    dropped: int = 0

    def __post_init__(self) -> None:
        # Rebuild the queue with the requested bound (dataclass default_factory can't see `buffer`).
        self.queue = asyncio.Queue(maxsize=self.buffer)

    def wants(self, event: Event) -> bool:
        """True if this subscription should receive ``event`` (channel + tenant filter, R4)."""
        if self.tenant is not None and event.tenant is not None and event.tenant != self.tenant:
            return False
        return any(fnmatch.fnmatchcase(event.channel, pat) for pat in self.patterns)

    def offer(self, event: Event) -> None:
        """Non-blocking enqueue with drop-oldest backpressure (R7)."""
        if self.queue.full():
            try:
                self.queue.get_nowait()  # drop the stalest event
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover — we just made room
            self.dropped += 1


class EventBus:
    """Process-local fan-out bus. One instance is shared by the app (see ``bus`` below)."""

    def __init__(self) -> None:
        self._subs: set[Subscription] = set()

    def subscribe(
        self, patterns: tuple[str, ...], *, tenant: str | None = None, buffer: int = DEFAULT_BUFFER
    ) -> Subscription:
        sub = Subscription(patterns=patterns, tenant=tenant, buffer=buffer)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, channel: str, data: dict, *, tenant: str | None = None) -> int:
        """Deliver an event to every matching subscription. Returns the number reached."""
        event = Event(channel=channel, data=data, tenant=tenant)
        reached = 0
        for sub in self._subs:
            if sub.wants(event):
                sub.offer(event)
                reached += 1
        return reached


# App-wide singleton (backend components import and publish onto this).
bus = EventBus()


def sse_frame(channel: str, data: dict) -> str:
    """Format one Server-Sent-Events frame (``event:``/``data:`` lines)."""
    import json

    return f"event: {channel}\ndata: {json.dumps(data)}\n\n"
