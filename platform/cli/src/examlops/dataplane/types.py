"""Value objects and errors for the dataplane (ADR 0130). No heavy imports at module level."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa

Watermark = dict[str, Any]
ResolvedConnection = dict[str, Any]


class DataplaneError(Exception):
    """Base error. Messages must be safe to show: no secrets, no URL userinfo."""


class ConnectorUnavailable(DataplaneError):
    """A connector's optional dependencies are not installed."""


class SpecError(DataplaneError):
    """A source spec, connection or name is invalid."""


class LimitExceeded(DataplaneError):
    """A pull exceeded max_rows, max_bytes or max_seconds."""


class EgressDenied(DataplaneError):
    """A connector tried to reach a host the egress policy forbids."""


class SnapshotNotFound(DataplaneError):
    """No committed snapshot matches the requested source/revision."""


class SnapshotIntegrityError(SnapshotNotFound):
    """A snapshot's bytes do not hash to its revision id: the data behind a pinned id was changed.

    A subclass of :class:`SnapshotNotFound` because, for a caller, the revision it asked for does
    not exist in the store any more — only something else stored under its name."""


class PullInProgress(DataplaneError):
    """Another pull of the same source holds the lock."""


class IncrementalInvalidated(DataplaneError):
    """An incremental read cannot be a pure append: data the parent snapshot already holds changed
    or disappeared upstream (a file rewritten in place, a file removed).

    Raised by a connector from ``read``; ``run_pull`` catches it and restarts the read as a full
    pull under the same pull id, so the new snapshot never carries the parent's stale parts."""


def _int_env(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else None


def _float_env(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else None


def _min(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


@dataclass(frozen=True)
class Limits:
    max_rows: int | None = None
    max_bytes: int | None = None
    max_seconds: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Limits:
        d = d or {}
        return cls(
            max_rows=int(d["max_rows"]) if d.get("max_rows") is not None else None,
            max_bytes=int(d["max_bytes"]) if d.get("max_bytes") is not None else None,
            max_seconds=float(d["max_seconds"]) if d.get("max_seconds") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None}

    def capped(self, other: Limits) -> Limits:
        rows = _min(self.max_rows, other.max_rows)
        size = _min(self.max_bytes, other.max_bytes)
        return Limits(
            max_rows=int(rows) if rows is not None else None,
            max_bytes=int(size) if size is not None else None,
            max_seconds=_min(self.max_seconds, other.max_seconds),
        )


def global_limits() -> Limits:
    """Platform-wide caps (``EXAMLOPS_DATAPLANE_MAX_ROWS/_BYTES/_SECONDS``); unset = no cap."""
    return Limits(
        max_rows=_int_env("EXAMLOPS_DATAPLANE_MAX_ROWS"),
        max_bytes=_int_env("EXAMLOPS_DATAPLANE_MAX_BYTES"),
        max_seconds=_float_env("EXAMLOPS_DATAPLANE_MAX_SECONDS"),
    )


@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str


@dataclass(frozen=True)
class TableInfo:
    name: str
    detail: str = ""


@dataclass(frozen=True)
class TableBatch:
    table: str
    batch: pa.RecordBatch
    watermark: Watermark | None = None
