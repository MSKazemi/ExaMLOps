"""Resilient SSE streaming primitives — resume cursors + backoff reconnect (Phase 1 item 1.7).

The dashboard's realtime bus is an in-process SSE singleton: a client that reconnects (or a second
uvicorn worker) loses events and hammers the server. This module is the transport-agnostic core that
fixes both — a **resume cursor** (Last-Event-ID) so a reconnecting client replays only what it missed
from a bounded ring buffer, and **exponential backoff with jitter** so N clients don't reconnect in a
thundering herd. It's pure (no I/O) so it's testable and reusable by a Redis/NATS-backed fan-out
(the cross-worker step) without change.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


def backoff_delay(
    attempt: int, *, base: float = 0.5, cap: float = 30.0, jitter: float = 0.5, rand: float = 0.5
) -> float:
    """Exponential backoff with full jitter for reconnect attempt ``attempt`` (0-based).

    Delay = ``min(cap, base * 2**attempt)`` then jittered by ±``jitter`` fraction using ``rand`` in
    [0,1) (injected so tests are deterministic; production passes a real random). Never negative.
    """
    raw = min(cap, base * (2 ** max(0, attempt)))
    # Full-jitter: scale within [1-jitter, 1+jitter] by rand.
    factor = (1.0 - jitter) + (2.0 * jitter) * max(0.0, min(1.0, rand))
    return max(0.0, raw * factor)


@dataclass
class _Event:
    id: int
    channel: str
    data: Any


class EventRing:
    """Bounded in-memory event ring with monotonic ids, backing SSE resume (Last-Event-ID).

    `publish` appends and returns the new event id; `since(cursor)` returns every event **after**
    ``cursor`` (a reconnecting client's Last-Event-ID) so it replays only the gap. The ring is
    bounded, so a client offline longer than the buffer gets a `truncated` signal and should do a
    full refresh instead of a partial replay.
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self._buf: deque[_Event] = deque(maxlen=maxlen)
        self._next_id = 1

    def publish(self, channel: str, data: Any) -> int:
        eid = self._next_id
        self._next_id += 1
        self._buf.append(_Event(eid, channel, data))
        return eid

    @property
    def latest_id(self) -> int:
        return self._next_id - 1

    def since(self, cursor: int | None) -> dict[str, Any]:
        """Events after ``cursor``. ``truncated`` = the cursor fell off the ring (do a full refresh)."""
        if cursor is None:
            events = list(self._buf)
            return {
                "events": [self._as_dict(e) for e in events],
                "truncated": False,
                "cursor": self.latest_id,
            }
        oldest = self._buf[0].id if self._buf else self._next_id
        truncated = cursor < oldest - 1  # asked for an id we no longer retain
        events = [e for e in self._buf if e.id > cursor]
        return {
            "events": [self._as_dict(e) for e in events],
            "truncated": truncated,
            "cursor": self.latest_id,
        }

    @staticmethod
    def _as_dict(e: _Event) -> dict[str, Any]:
        return {"id": e.id, "channel": e.channel, "data": e.data}


def parse_last_event_id(header: str | None) -> int | None:
    """Parse a ``Last-Event-ID`` header into an int cursor, or None if absent/invalid."""
    if not header:
        return None
    try:
        return int(header.strip())
    except ValueError:
        return None
