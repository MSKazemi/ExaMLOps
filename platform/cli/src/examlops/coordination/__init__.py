"""Externalized coordination — distributed locks, idempotency, rate limits (Phase 1 item 1.2).

The control plane today keeps dedup/idempotency/rate-limit/breaker state **in process memory**, so a
second replica behind a load balancer double-fires retrains, re-processes duplicate webhooks, and
each enforces its own rate limit. This is the seam that fixes that: coordination state moves behind a
``Coordinator`` interface.

  * ``db`` (default) — backed by ``platform_db`` tables, so it already coordinates across every
    **process** sharing ``platform.db`` (CLI, control plane, agent, bridge). Correct for single-host
    multi-process today; no new dependency.
  * ``redis`` — the cross-**host** HA backend for multiple replicas on different machines. A thin
    skeleton that fails loudly until ``redis`` + ``EXAMLOPS_REDIS_URL`` are configured, so callers can
    adopt the interface now and flip the backend when Redis is stood up — zero call-site change.

Same degrade-gracefully DNA as the event publisher (1.3) and StorageBackend (0.1) seams.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class Coordinator(Protocol):
    """Cross-replica coordination primitives."""

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool: ...
    def unlock(self, key: str, holder: str) -> None: ...
    def first_seen(self, key: str, ttl_s: float) -> bool: ...
    def allow(self, bucket: str, limit: int, window_s: float) -> bool: ...


class DbCoordinator:
    """Default coordinator backed by ``platform_db`` (cross-process on a shared ``platform.db``)."""

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        from examlops.data import init_db
        from examlops.data.coordination import coord_try_lock

        init_db()
        return coord_try_lock(key, holder, ttl_s)

    def unlock(self, key: str, holder: str) -> None:
        from examlops.data import init_db
        from examlops.data.coordination import coord_unlock

        init_db()
        coord_unlock(key, holder)

    def first_seen(self, key: str, ttl_s: float) -> bool:
        """True the first time ``key`` is seen within its TTL (i.e. NOT a duplicate)."""
        from examlops.data import init_db
        from examlops.data.coordination import coord_check_and_set_idempotent

        init_db()
        return coord_check_and_set_idempotent(key, ttl_s)

    def allow(self, bucket: str, limit: int, window_s: float) -> bool:
        from examlops.data import init_db
        from examlops.data.coordination import coord_rate_allow

        init_db()
        return coord_rate_allow(bucket, limit, window_s)


class RedisCoordinator:
    """Cross-host HA coordinator — skeleton until ``redis`` + ``EXAMLOPS_REDIS_URL`` are wired.

    Fails loudly rather than silently degrading to no coordination (which would reintroduce the
    double-fire bug). Implement with ``SET NX PX`` locks, ``SET NX`` idempotency keys, and an
    ``INCR``+``EXPIRE`` rate limiter when Redis is available.
    """

    def _unavailable(self) -> RuntimeError:
        return RuntimeError(
            "redis coordinator is not configured — set EXAMLOPS_REDIS_URL and install redis, "
            "or use EXAMLOPS_COORDINATOR=db (default). Refusing to run without coordination."
        )

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        raise self._unavailable()

    def unlock(self, key: str, holder: str) -> None:
        raise self._unavailable()

    def first_seen(self, key: str, ttl_s: float) -> bool:
        raise self._unavailable()

    def allow(self, bucket: str, limit: int, window_s: float) -> bool:
        raise self._unavailable()


_BACKENDS: dict[str, type] = {"db": DbCoordinator, "redis": RedisCoordinator}
_coordinator: Coordinator | None = None


def get_coordinator() -> Coordinator:
    """Resolve the configured coordinator (``EXAMLOPS_COORDINATOR``, default ``db``). Cached."""
    global _coordinator
    if _coordinator is None:
        name = os.getenv("EXAMLOPS_COORDINATOR", "db").strip().lower()
        _coordinator = _BACKENDS.get(name, DbCoordinator)()
    return _coordinator


def reset_coordinator() -> None:
    """Clear the cached coordinator (tests / after an env change)."""
    global _coordinator
    _coordinator = None
