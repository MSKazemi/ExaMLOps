"""Where a serving replica gets the serving snapshot (ADR 0127, plan P4.2).

Sources, in order of freshness:

* **NATS KV** (``examlops-serving``/``snapshot``) when the event backbone is NATS;
* **the platform database** (``serving_snapshots``), the source of truth;
* **a local last-known-good file** (``RAY_SNAPSHOT_CACHE``), so a replica that restarts while the
  control plane, the database and the broker are all down still comes up serving what it served
  (static stability, ADR 0123).

Every reachable source is asked and the highest generation wins — the KV mirror is best-effort
and can trail the database. A snapshot whose digest does not match its content is refused, from
any source: a replica acting on a corrupted snapshot would unload models it should serve.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("ray_serving.snapshot")


def _default_cache() -> Path:
    return Path(tempfile.gettempdir()) / "examlops-serving-snapshot.json"


def verified(snapshot: Any) -> dict[str, Any] | None:
    """``snapshot`` if it is well-formed and its digest matches its content, else ``None``."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("generation"), int):
        return None
    try:
        from examlops.serving_snapshot import digest_of  # noqa: PLC0415

        content = {k: snapshot.get(k, {}) for k in ("models", "traffic", "shadow")}
        if digest_of(content) != snapshot.get("digest"):
            logger.error("Refusing serving snapshot %s: digest mismatch", snapshot["generation"])
            return None
    except Exception as exc:  # noqa: BLE001 - an unverifiable snapshot is not used
        logger.error("Cannot verify serving snapshot: %s", exc)
        return None
    return snapshot


class SnapshotReader:
    def __init__(self, cache_path: str | os.PathLike[str] | None = None) -> None:
        self.cache_path = Path(cache_path or os.getenv("RAY_SNAPSHOT_CACHE") or _default_cache())
        self.source: str | None = None
        self._persisted: int | None = None
        self._last: dict[str, Any] | None = None

    # -- sources -------------------------------------------------------------------------
    @staticmethod
    def _from_kv() -> dict[str, Any] | None:
        if os.getenv("EXAMLOPS_EVENT_PUBLISHER", "log").strip().lower() != "nats":
            return None
        from examlops.events import nats_backend  # noqa: PLC0415
        from examlops.serving_snapshot import KV_BUCKET, KV_KEY  # noqa: PLC0415

        found = nats_backend.shared().kv_get(KV_BUCKET, KV_KEY)
        return json.loads(found[0]) if found else None

    def _from_db(self) -> dict[str, Any] | None:
        from examlops.serving_snapshot import latest, latest_generation  # noqa: PLC0415

        # One indexed MAX() per poll; the body is read only when a new generation exists.
        generation = latest_generation()
        if generation is None:
            return None
        if self._last is not None and self._last.get("generation") == generation:
            return self._last
        return latest()

    def _from_cache(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- the answer ----------------------------------------------------------------------
    def newest(self) -> dict[str, Any] | None:
        """The freshest verified snapshot any source can give, or ``None`` if none exists."""
        best: dict[str, Any] | None = None
        best_source: str | None = None
        answered = False  # did any source answer at all, even with "none published"?
        for name, fetch in (("kv", self._from_kv), ("db", self._from_db)):
            try:
                raw = fetch()
            except Exception as exc:  # noqa: BLE001 - an unreachable source is skipped
                logger.debug("serving snapshot source %s unavailable: %s", name, exc)
                continue
            answered = answered or name == "db"  # the database is authoritative for "none"
            candidate = verified(raw)
            if candidate is not None and (
                best is None or candidate["generation"] > best["generation"]
            ):
                best, best_source = candidate, name
        if best is None:
            if answered:  # the platform has no snapshot; an old file must not stand in for one
                self.source = None
                return None
            cached = verified(self._from_cache())
            if cached is not None:
                self.source = "cache"
            return cached
        self.source = best_source
        self._last = best
        self._persist(best)
        return best

    def _persist(self, snapshot: dict[str, Any]) -> None:
        if snapshot["generation"] == self._persisted:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.cache_path)  # atomic: a crash never leaves half a file
            self._persisted = snapshot["generation"]
        except OSError as exc:
            logger.warning("Could not persist serving snapshot to %s: %s", self.cache_path, exc)
