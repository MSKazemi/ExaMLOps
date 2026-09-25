"""Online-store seam for the feature store (ADR 0017 clause 1).

The durable online store is the ``online_features`` table in ``platform.db`` — materialization
always writes there first, and it stays the source a Redis tier is rebuilt from. With
``EXAMLOPS_FEATURE_ONLINE_STORE=redis`` a :class:`RedisOnlineStore` sits in front of it as the
low-latency serving tier (the Feast-style Redis online store this ADR named):

* **writes** are mirrored to Redis after the durable write; a Redis failure is *reported*, never
  raised, and never undoes the durable materialization. The keys a failed mirror did not update
  are deleted, so reads fall through to the table instead of serving the superseded value;
* **reads** try Redis first and fall back to the durable table on a miss or any Redis error
  ("dev degrade-to-offline", ADR 0017). The one gap: when Redis is unreachable for the write
  *and* the invalidation but reachable for later reads, it serves the previous value until the
  next successful mirror or the key's expiry. The mirror error says so.

``redis`` is an optional dependency (``examlops[features-online]``), imported lazily only when the
Redis tier is selected. Without it — or without ``EXAMLOPS_FEATURE_REDIS_URL`` /
``EXAMLOPS_REDIS_URL`` — selection degrades to the durable table and says why.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Rows per Redis pipeline flush — bounds one round trip's payload.
REDIS_BATCH = 500


class OnlineStore(Protocol):
    backend: str

    def read(self, view: str, entity_id: str) -> dict[str, Any] | None: ...

    def write(
        self, view: str, rows: list[dict[str, Any]], ttl_seconds: int = 0
    ) -> MirrorResult: ...


@dataclass
class MirrorResult:
    """What a mirror write to the serving tier did."""

    backend: str
    written: int = 0
    error: str | None = None
    #: Keys deleted after a failed mirror so reads fall back to the durable table.
    invalidated: int = 0


@dataclass
class DbOnlineStore:
    """The durable ``online_features`` table — always present, always the source of truth."""

    backend: str = "db"

    def read(self, view: str, entity_id: str) -> dict[str, Any] | None:
        from examlops import data as platform_db

        return platform_db.get_online_feature(view, entity_id)

    def write(self, view: str, rows: list[dict[str, Any]], ttl_seconds: int = 0) -> MirrorResult:
        # Materialization already wrote these rows here; there is nothing to mirror.
        return MirrorResult(backend=self.backend, written=0)


@dataclass
class RedisOnlineStore:
    """Redis serving tier in front of :class:`DbOnlineStore`.

    Keys are ``<prefix>:<view>:<entity_id>`` holding ``{"event_ts", "values"}`` JSON. A view TTL
    (seconds) becomes the key expiry, which bounds Redis memory to recently materialized entities.
    It is **not** a serving-freshness guarantee: an expired key is a miss, and a miss reads the
    durable table, which still holds the last materialized value. Staleness is reported by the
    freshness monitor (``FeatureViewStale``), not enforced on reads.
    """

    client: Any
    prefix: str = "examlops:features"
    fallback: DbOnlineStore = field(default_factory=DbOnlineStore)
    backend: str = "redis"
    errors: int = 0

    def _key(self, view: str, entity_id: str) -> str:
        return f"{self.prefix}:{view}:{entity_id}"

    def read(self, view: str, entity_id: str) -> dict[str, Any] | None:
        try:
            raw = self.client.get(self._key(view, entity_id))
        except Exception as exc:  # noqa: BLE001 - degrade to the durable table
            self.errors += 1
            logger.warning("redis online read failed for %s/%s: %s", view, entity_id, exc)
            return self.fallback.read(view, entity_id)
        if raw is None:
            return self.fallback.read(view, entity_id)
        try:
            doc = json.loads(raw)
            values = doc.get("values")
            return values if isinstance(values, dict) else self.fallback.read(view, entity_id)
        except (TypeError, ValueError):
            return self.fallback.read(view, entity_id)

    def write(self, view: str, rows: list[dict[str, Any]], ttl_seconds: int = 0) -> MirrorResult:
        result = MirrorResult(backend=self.backend)
        try:
            for i in range(0, len(rows), REDIS_BATCH):
                pipe = self.client.pipeline()
                for row in rows[i : i + REDIS_BATCH]:
                    payload = json.dumps(
                        {"event_ts": str(row.get("event_ts")), "values": row.get("values") or {}}
                    )
                    key = self._key(view, str(row["entity_id"]))
                    if ttl_seconds > 0:
                        pipe.set(key, payload, ex=int(ttl_seconds))
                    else:
                        pipe.set(key, payload)
                pipe.execute()
                result.written += len(rows[i : i + REDIS_BATCH])
        except Exception as exc:  # noqa: BLE001 - reported, never raised (module docstring)
            self.errors += 1
            result.error = f"{type(exc).__name__}: {exc}"
            self.invalidate(view, rows[result.written :], result)
        return result

    def invalidate(self, view: str, rows: list[dict[str, Any]], result: MirrorResult) -> bool:
        """Delete the keys a failed mirror did not update. Best-effort; ``False`` if it failed.

        The durable table already holds the new values. A key left in place would keep serving
        the value it replaced, because a Redis hit never consults the table. A Redis that refuses
        writes (``maxmemory`` with ``noeviction``, a read-only replica) still accepts DEL, so
        this closes the common case. When Redis is unreachable the DEL fails too: the error says
        so, and the old keys stay until the next successful mirror or their expiry.
        """
        try:
            for i in range(0, len(rows), REDIS_BATCH):
                keys = [self._key(view, str(r["entity_id"])) for r in rows[i : i + REDIS_BATCH]]
                pipe = self.client.pipeline()
                pipe.delete(*keys)
                pipe.execute()
                result.invalidated += len(keys)
        except Exception as exc:  # noqa: BLE001 - reported in the result, never raised
            result.error = (
                f"{result.error}; invalidating the stale keys also failed "
                f"({type(exc).__name__}: {exc}), so Redis may serve superseded values until "
                "the next successful mirror or key expiry"
            )
            return False
        return True


_lock = threading.Lock()
_cached: OnlineStore | None = None
_selection_note: str = ""


def selection_note() -> str:
    """Why the current online store was chosen (e.g. why Redis was *not*)."""
    return _selection_note


def reset_online_store() -> None:
    global _cached, _selection_note
    with _lock:
        _cached = None
        _selection_note = ""


def select_online_store() -> OnlineStore:
    """The configured online store, cached per process. Never raises: degrades to the table."""
    global _cached, _selection_note
    with _lock:
        if _cached is not None:
            return _cached
        choice = os.getenv("EXAMLOPS_FEATURE_ONLINE_STORE", "db").strip().lower() or "db"
        store: OnlineStore = DbOnlineStore()
        note = "durable platform.db table"
        if choice == "redis":
            url = (
                os.getenv("EXAMLOPS_FEATURE_REDIS_URL", "").strip()
                or os.getenv("EXAMLOPS_REDIS_URL", "").strip()
            )
            if not url:
                note = "redis selected but EXAMLOPS_FEATURE_REDIS_URL/EXAMLOPS_REDIS_URL unset"
            else:
                try:
                    import redis  # optional: examlops[features-online]

                    timeout = float(os.getenv("EXAMLOPS_FEATURE_REDIS_TIMEOUT", "0.5"))
                    client = redis.Redis.from_url(
                        url, socket_timeout=timeout, socket_connect_timeout=timeout
                    )
                    store = RedisOnlineStore(
                        client=client,
                        prefix=os.getenv("EXAMLOPS_FEATURE_REDIS_PREFIX", "examlops:features"),
                    )
                    note = "redis serving tier over the durable platform.db table"
                except ImportError:
                    note = "redis selected but the 'redis' package is missing (examlops[features-online])"
                except Exception as exc:  # noqa: BLE001 - a bad URL degrades, loudly
                    note = f"redis selected but unusable ({type(exc).__name__}: {exc})"
        elif choice != "db":
            note = f"unknown EXAMLOPS_FEATURE_ONLINE_STORE={choice!r}; using the durable table"
        if store.backend == "db" and choice != "db":
            logger.warning("feature online store degraded to platform.db: %s", note)
        _cached, _selection_note = store, note
        return store


__all__ = [
    "DbOnlineStore",
    "MirrorResult",
    "OnlineStore",
    "RedisOnlineStore",
    "reset_online_store",
    "select_online_store",
    "selection_note",
]
