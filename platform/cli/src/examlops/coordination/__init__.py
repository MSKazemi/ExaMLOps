"""Externalized coordination — distributed locks, idempotency, rate limits (Phase 1 item 1.2).

The control plane today keeps dedup/idempotency/rate-limit/breaker state **in process memory**, so a
second replica behind a load balancer double-fires retrains, re-processes duplicate webhooks, and
each enforces its own rate limit. This is the seam that fixes that: coordination state moves behind a
``Coordinator`` interface.

  * ``db`` (default) — backed by ``platform_db`` tables, so it already coordinates across every
    **process** sharing ``platform.db`` (CLI, control plane, agent, bridge). Correct for single-host
    multi-process today; no new dependency.
  * ``redis`` — the cross-**host** HA backend for multiple replicas on different machines. It uses
    atomic Redis operations for leases, deduplication, and fixed-window rate limits.

Same degrade-gracefully DNA as the event publisher (1.3) and StorageBackend (0.1) seams.
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable


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
    """Cross-host coordinator built from atomic Redis primitives.

    Lock renewal and release compare the holder value inside Lua scripts; one replica therefore
    cannot extend or delete another replica's lease after its own lease expires. Keys are namespaced
    so a shared Redis cluster can safely serve more than one ExaMLOps installation.
    """

    _LOCK_SCRIPT = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      redis.call('pexpire', KEYS[1], ARGV[2])
      return 1
    end
    if redis.call('set', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
      return 1
    end
    return 0
    """
    _UNLOCK_SCRIPT = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    _RATE_SCRIPT = """
    local count = redis.call('incr', KEYS[1])
    if count == 1 then
      redis.call('pexpire', KEYS[1], ARGV[1])
    end
    return count
    """

    def __init__(self, client: Any | None = None) -> None:
        self._prefix = os.getenv("EXAMLOPS_REDIS_PREFIX", "examlops:coord").strip(":")
        if client is not None:
            self._client = client
            return

        url = os.getenv("EXAMLOPS_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError(
                "redis coordinator is not configured — set EXAMLOPS_REDIS_URL or use "
                "EXAMLOPS_COORDINATOR=db. Refusing to run without coordination."
            )
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "redis coordinator requires the 'redis' package; install 'examlops[coordination]'"
            ) from exc
        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )

    @staticmethod
    def _ttl_ms(ttl_s: float) -> int:
        if ttl_s <= 0:
            raise ValueError("coordination TTL/window must be greater than zero")
        return max(1, int(ttl_s * 1000))

    def _key(self, kind: str, value: str) -> str:
        return f"{self._prefix}:{kind}:{value}"

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        result = self._client.eval(
            self._LOCK_SCRIPT,
            1,
            self._key("lock", key),
            holder,
            self._ttl_ms(ttl_s),
        )
        return bool(result)

    def unlock(self, key: str, holder: str) -> None:
        self._client.eval(self._UNLOCK_SCRIPT, 1, self._key("lock", key), holder)

    def first_seen(self, key: str, ttl_s: float) -> bool:
        return bool(
            self._client.set(
                self._key("seen", key),
                "1",
                nx=True,
                px=self._ttl_ms(ttl_s),
            )
        )

    def allow(self, bucket: str, limit: int, window_s: float) -> bool:
        if limit <= 0:
            return False
        count = self._client.eval(
            self._RATE_SCRIPT,
            1,
            self._key("rate", bucket),
            self._ttl_ms(window_s),
        )
        return int(count) <= limit


_BACKENDS: dict[str, type] = {"db": DbCoordinator, "redis": RedisCoordinator}
_coordinator: Coordinator | None = None


def get_coordinator() -> Coordinator:
    """Resolve the configured coordinator (``EXAMLOPS_COORDINATOR``, default ``db``). Cached."""
    global _coordinator
    if _coordinator is None:
        name = os.getenv("EXAMLOPS_COORDINATOR", "db").strip().lower()
        cls = _BACKENDS.get(name)
        if cls is None:
            supported = ", ".join(sorted(_BACKENDS))
            raise RuntimeError(
                f"unsupported EXAMLOPS_COORDINATOR={name!r}; expected one of: {supported}"
            )
        _coordinator = cls()
    return _coordinator


def reset_coordinator() -> None:
    """Clear the cached coordinator (tests / after an env change)."""
    global _coordinator
    _coordinator = None
