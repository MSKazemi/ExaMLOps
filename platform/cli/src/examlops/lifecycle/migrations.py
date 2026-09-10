"""Ordered, versioned data migrations for the instance-data layer (ADR 0128).

The platform schema has always evolved *additively* — ``CREATE TABLE IF NOT EXISTS`` plus the
ADD-only ``_COLUMN_MIGRATIONS`` in :mod:`examlops.platform_db` — and that stays the way new tables
and columns arrive. What additive DDL cannot express is a change to the *data*: a backfill, a
re-keying, a value rewrite. Those are migrations, and this module is their registry.

Each migration has a monotonically increasing integer ``version``; the highest one is the code's
**data format**. Two properties decide how a migration may run:

* ``online`` — idempotent and safe to apply at process start while other replicas keep running.
  :func:`examlops.lifecycle.dataformat.on_schema_ready` applies pending online migrations
  automatically, so an upgraded service converges the data with no operator step.
* ``breaking`` — after it runs, *older* code can no longer read the data correctly (a contract
  step of expand/contract). It raises the data's ``min_reader_format``, which is what makes an
  older release refuse the data instead of misreading it. A breaking migration is never online:
  it only runs through ``exa upgrade apply``, which takes a verified backup first.

The registry is append-only. Never edit or remove a released migration — a site that has not
upgraded yet still needs to run it exactly as it shipped.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


def _noop(_conn: Any) -> None:
    """The baseline: every pre-0128 instance's schema is already what format 1 means."""


@dataclass(frozen=True)
class Migration:
    """One step of the data format. ``apply`` receives an open datastore connection."""

    version: int
    name: str
    description: str
    apply: Callable[[Any], None] = _noop
    online: bool = True
    breaking: bool = False


#: The registry, in version order. Format 1 is the baseline every existing instance is adopted at.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "baseline",
        "Adopt the schema as of v0.52: additive DDL + _COLUMN_MIGRATIONS, no data rewrite.",
    ),
)


def validate(migrations: Sequence[Migration] = MIGRATIONS) -> None:
    """Refuse a malformed registry: versions 1..N contiguous and unique, breaking ⇒ not online."""
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"migration versions must be 1..N in order, got {versions}")
    names = [m.name for m in migrations]
    if len(names) != len(set(names)):
        raise ValueError("migration names must be unique")
    for m in migrations:
        if m.breaking and m.online:
            raise ValueError(
                f"migration {m.version} ({m.name}) is breaking, so it cannot be online"
            )


def code_format(migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """The data format this code writes: the highest registered migration version."""
    return max((m.version for m in migrations), default=0)


def min_reader_after(current_min_reader: int, applied: Sequence[Migration]) -> int:
    """The ``min_reader_format`` once ``applied`` have run: the latest breaking version wins."""
    return max([current_min_reader, *(m.version for m in applied if m.breaking)])


def pending(data_format: int, migrations: Sequence[Migration] = MIGRATIONS) -> list[Migration]:
    """Migrations newer than ``data_format``, in the order they must run."""
    return [m for m in migrations if m.version > data_format]
