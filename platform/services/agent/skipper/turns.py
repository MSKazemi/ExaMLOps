"""Cross-replica serialization for checkpointed agent turns."""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from typing import Any

from skipper import config

log = logging.getLogger(__name__)


class TurnBusy(RuntimeError):
    """Another request currently owns the same checkpoint thread."""


class TurnCoordinationUnavailable(RuntimeError):
    """The shared coordinator could not safely arbitrate a turn."""


class TurnLease:
    """A renewable, finite distributed lease owned by one graph invocation."""

    def __init__(self, coordinator: Any, key: str, holder: str, ttl_s: float) -> None:
        self._coordinator = coordinator
        self.key = key
        self.holder = holder
        self.ttl_s = ttl_s
        self._stop = threading.Event()
        self._released = threading.Event()
        self._worker_claimed = threading.Event()
        self._release_guard = threading.Lock()
        self._renewal = threading.Thread(
            target=self._renew,
            name="agent-turn-lease",
            daemon=True,
        )
        self._renewal.start()

    def claim_by_worker(self) -> None:
        """Mark that a graph worker now owns release, even if its client disconnects."""
        self._worker_claimed.set()

    def release_if_unclaimed(self) -> None:
        """Release a response that ended before its graph worker could start."""
        if not self._worker_claimed.is_set():
            self.release()

    def _renew(self) -> None:
        interval = min(30.0, max(0.1, self.ttl_s / 3))
        while not self._stop.wait(interval):
            try:
                if not self._coordinator.try_lock(self.key, self.holder, self.ttl_s):
                    log.error("Agent turn lease was lost before the graph completed")
                    return
            except Exception:
                # The existing finite lease remains authoritative. Do not silently substitute a
                # process-local lock: another replica must recover only after this lease expires.
                log.exception("Unable to renew agent turn lease")
                return

    def release(self) -> None:
        """Stop renewal and release only this invocation's lease."""
        with self._release_guard:
            if self._released.is_set():
                return
            self._released.set()
        self._stop.set()
        if threading.current_thread() is not self._renewal:
            self._renewal.join()
        try:
            self._coordinator.unlock(self.key, self.holder)
        except Exception:
            # A failed release remains fail-safe: the bounded lease expires naturally.
            log.exception("Unable to release agent turn lease; waiting for its TTL")


def acquire_turn(
    thread_id: str,
    *,
    coordinator: Any | None = None,
    ttl_s: float | None = None,
) -> TurnLease:
    """Acquire the one active graph-turn lease for ``thread_id`` or fail closed."""
    if coordinator is None:
        try:
            from examlops.coordination import get_coordinator

            coordinator = get_coordinator()
        except Exception as exc:
            raise TurnCoordinationUnavailable("Agent turn coordination is unavailable") from exc

    ttl = config.AGENT_TURN_LEASE_SECONDS if ttl_s is None else max(1.0, ttl_s)
    # The checkpoint id is already server-scoped, but hashing keeps caller labels out of shared
    # coordination keys and gives Redis/SQL a fixed-size key.
    key = f"agent-turn:{hashlib.sha256(thread_id.encode()).hexdigest()}"
    holder = secrets.token_urlsafe(24)
    try:
        acquired = coordinator.try_lock(key, holder, ttl)
    except Exception as exc:
        raise TurnCoordinationUnavailable("Agent turn coordination is unavailable") from exc
    if not acquired:
        raise TurnBusy("Another request is already running for this session")
    return TurnLease(coordinator, key, holder, ttl)
