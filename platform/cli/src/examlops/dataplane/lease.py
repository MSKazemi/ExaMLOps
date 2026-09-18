"""A coordinator-backed lease heartbeat (ADR 0130 dataplane pull locks).

Extracted out of ``examlops.dataplane.pull`` (task A4, Plan 2 batch S1) so a second holder of a
coordinator lock — stream leader election is the first one lined up — can reuse the same
renew-until-lost mechanism instead of re-implementing it.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from examlops.dataplane.safety import redact

logger = logging.getLogger(__name__)

# How long stop() waits, by default, for an in-flight renewal before releasing anyway (a late
# renewal then gives the lock straight back itself, since unlock is holder-checked). A caller with
# its own tuning (``pull.py``'s ``_HEARTBEAT_JOIN_S``) passes its own ``join_s``.
DEFAULT_JOIN_S = 10.0

#: The shortest TTL a fenced lease accepts: below it the fence leaves no time to stop the owner.
MIN_FENCED_TTL_S = 5.0

#: How long the fence watchdog sleeps at a stretch before re-reading the clock. It never delays
#: the fence (the deadline is still `_last_ok + fence_after`); it bounds how stale the watchdog's
#: view of the clock can be, which is what lets a test inject a clock and see the fence fire
#: without waiting out a real TTL (review P5).
_FENCE_POLL_S = 1.0


def fence_after(ttl_s: float) -> float:
    """Seconds after a successful renewal was SENT at which a fenced lease counts itself lost:
    ``min(0.8 × ttl_s, floor(ttl_s) − 2)`` — the one place this is decided.

    The coordinator's own clock sets how long the key really lives. The default ``DbCoordinator``
    stores ``floor(now) + int(ttl_s)`` in whole seconds and compares against a whole-second
    ``CURRENT_TIMESTAMP``, so a key written at time *t* (≥ the send time) can be taken by another
    holder from just after ``t + floor(ttl_s) − 1``: one second goes to the integer truncation,
    up to one more to the timestamp resolution. Fencing at ``floor(ttl_s) − 2`` after the send
    therefore leaves at least a second for the owner's run to stop before anyone else may start;
    ``0.8 × ttl_s`` is the tighter bound for long TTLs (15 s → 12 s). An exact-TTL coordinator
    (Redis) only gains margin. Needs ``ttl_s ≥`` :data:`MIN_FENCED_TTL_S`.
    """
    return min(0.8 * ttl_s, math.floor(ttl_s) - 2.0)


class LeaseHeartbeat:
    """Keeps a held coordinator lock alive: re-takes it (same holder, same TTL — the coordinator's
    own re-entrant renew) every ``ttl_s / 3`` on a daemon thread until :meth:`stop`.

    Without it a lease covers only the first TTL its holder took: once it lapses, another holder
    can take the lock while this one is still working. ``lost`` turns true when a renewal is
    refused — another holder has the lock, because this process stalled past the lease — and the
    owner must then not commit / proceed. A renewal that *errors* (a datastore hiccup) is retried
    on the next beat; the lease still has two thirds of its TTL left.

    ``on_lost``, when given, is called at most once — from the heartbeat thread, the moment
    ``lost`` is set true — with no arguments. ``lost`` is recorded *before* the callback runs, so
    a caller polling ``lost`` sees the lost state even if the callback itself misbehaves; an
    exception the callback raises is caught and logged, never left to kill the heartbeat thread
    silently.

    ``clock`` (default :func:`time.monotonic`) is the one time source for the fence: the send
    time of each renewal, ``acquired_at``, and the watchdog's deadline all read it, so a test can
    drive the fence exactly instead of waiting out a TTL. The renew *beat* itself
    (``ttl_s / 3``) stays on real time — it is a sleep, not a decision.

    ``fence_on_error=True`` (stream leader election, ADR 0131 d8) closes the gap an *erroring*
    coordinator leaves open: a holder partitioned from the coordinator cannot renew, its key
    expires after the TTL and another holder takes it — while this one, only ever seeing errors,
    would keep running. With the fence, no successful renewal for :func:`fence_after` seconds —
    renewals refused, raising or hanging alike — marks the lease lost and calls ``on_lost``
    (once), before the key can expire. The clock runs from when each successful renewal was
    *sent* (a slow reply must not eat the margin), and from ``acquired_at`` — the send time of
    the caller's acquiring ``try_lock`` — for the first one. A watchdog thread keeps the deadline
    even while a renewal call hangs; a renewal that succeeds only after the fence gives the lock
    straight back. A fenced lease needs ``ttl_s ≥ 5``. The default (``False``) is the pull
    locks' behaviour, unchanged: an error is retried on the next beat.

    The heartbeat only ever renews and unlocks as ``holder``; give each acquisition its own
    holder (a fencing token) and a late renewal or unlock of an old lease can never touch a newer
    one, since both coordinators condition ``try_lock`` and ``unlock`` on the holder.
    """

    def __init__(
        self,
        coord: Any,
        key: str,
        holder: str,
        ttl_s: float,
        on_lost: Callable[[], None] | None = None,
        *,
        join_s: float = DEFAULT_JOIN_S,
        fence_on_error: bool = False,
        acquired_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if fence_on_error and ttl_s < MIN_FENCED_TTL_S:
            raise ValueError(
                f"a fenced lease needs ttl_s >= {MIN_FENCED_TTL_S:g} seconds, not {ttl_s}"
            )
        self._coord = coord
        self._key = key
        self._holder = holder
        self._ttl_s = ttl_s
        self._on_lost = on_lost
        self._join_s = join_s
        self._clock = clock
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="dataplane-lease", daemon=True)
        self._mu = threading.Lock()
        self._fired = False
        # The caller's acquisition is the first successful "renewal", timed from when it was sent.
        self._last_ok = clock() if acquired_at is None else acquired_at
        self._fence_after = fence_after(ttl_s) if fence_on_error else None
        self._watch = (
            threading.Thread(target=self._watchdog, name="dataplane-lease-fence", daemon=True)
            if fence_on_error
            else None
        )

    def start(self) -> LeaseHeartbeat:
        self._thread.start()
        if self._watch is not None:
            self._watch.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._ttl_s / 3.0):
            sent = self._clock()  # the coordinator's expiry runs from about now, not the reply
            try:
                renewed = self._coord.try_lock(self._key, self._holder, ttl_s=self._ttl_s)
            except Exception as exc:  # noqa: BLE001 — a transient error: try again next beat
                logger.warning(
                    "dataplane: could not renew the lease on %s: %s",
                    self._key,
                    redact(f"{type(exc).__name__}: {exc}"),
                )
                continue
            if self._stop.is_set():
                # stop() stopped waiting for this renewal and the owner may already have released
                # the lock: give back what a late renewal just re-took (unlock is holder-checked).
                if renewed:
                    self._release()
                return
            if not renewed:
                self._lose("dataplane: lost the lease on %s to another holder")
                return
            if self._fence_after is not None:
                with self._mu:
                    fenced = self.lost
                    if not fenced:
                        self._last_ok = max(self._last_ok, sent)
                if fenced:
                    # The owner already stopped on the fence: give back what this late renewal
                    # re-took, so the key is free for the next holder at once.
                    self._release()
                    return

    def _lose(self, message: str) -> None:
        """Mark the lease lost and call ``on_lost`` — once, whoever gets here first."""
        with self._mu:
            if self._fired:
                return
            self._fired = True
            self.lost = True
        logger.warning(message, self._key)
        if self._on_lost is not None:
            try:
                self._on_lost()
            except Exception as exc:  # noqa: BLE001 — lost is already recorded above; a
                # misbehaving callback must never take the heartbeat thread down silently.
                logger.warning(
                    "dataplane: on_lost callback for %s raised: %s",
                    self._key,
                    redact(f"{type(exc).__name__}: {exc}"),
                )

    def _watchdog(self) -> None:
        """``fence_on_error``: fence once :func:`fence_after` passes with no successful renewal."""
        assert self._fence_after is not None
        while True:
            with self._mu:
                if self.lost:
                    return
                remaining = self._last_ok + self._fence_after - self._clock()
            if remaining <= 0:
                self._lose(
                    "dataplane: no successful renewal of the lease on %s in time; "
                    "fencing it before it can expire"
                )
                return
            # Sleep in slices, so the deadline is decided by the clock and not by how long this
            # thread happened to be asleep when it was set (review P5).
            if self._stop.wait(min(remaining, _FENCE_POLL_S)):
                return

    def _release(self) -> None:
        try:
            self._coord.unlock(self._key, self._holder)
        except Exception as exc:  # noqa: BLE001 — the lease TTL frees it anyway
            logger.warning(
                "dataplane: could not release %s: %s",
                self._key,
                redact(f"{type(exc).__name__}: {exc}"),
            )

    def stop(self) -> None:
        """Stop renewing. Waits up to ``join_s`` for an in-flight renewal, so the caller's release
        normally lands last; a renewal that outlasts the wait (a hung datastore) releases the lock
        itself once it returns, so it can never keep a released lock."""
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._join_s)
        watch = self._watch
        if watch is not None and watch.is_alive() and watch is not threading.current_thread():
            watch.join(timeout=self._join_s)  # it wakes on the stop event at once


__all__ = ["DEFAULT_JOIN_S", "MIN_FENCED_TTL_S", "LeaseHeartbeat", "fence_after"]
