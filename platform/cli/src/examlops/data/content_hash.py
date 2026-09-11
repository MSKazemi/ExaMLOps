"""Content-addressing primitives shared by dataset versioning (ADR 0003) and the dataplane (ADR 0130).

One formula, two callers. A dataplane pull computes a snapshot's revision while it writes the
parts; ``pipelines.datasets.versioning.resolve_revision`` computes a revision over the same files
after a training run downloads them. Because both use these functions, the two ids are equal,
and a run can prove it trains on the snapshot it pinned.

Ordering contract: callers pass schema parts in **sorted path order** (``PurePath`` component
order) and digests in any order (``revision_id`` sorts them).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB streaming read — never load a multi-GB parquet into memory.


def file_digest(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def schema_part(path: Path) -> str | None:
    """``name:type|name:type`` for a parquet file, or None when it cannot be read as parquet."""
    try:
        import pyarrow.parquet as pq
    except Exception:  # pragma: no cover - pyarrow is present wherever this runs
        return None
    try:
        schema = pq.read_schema(path)
    except Exception:
        return None
    return "|".join(f"{name}:{schema.field(name).type}" for name in schema.names)


def schema_hash(parts: Iterable[str | None]) -> str:
    """Hash of the schema parts, skipping unreadable files; ``""`` when nothing was readable."""
    kept = [p for p in parts if p]
    if not kept:
        return ""
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()


def revision_id(digests: Iterable[str], schema_hash_value: str) -> str:
    """``sha256(sorted digests ‖ schema hash)`` — deterministic and order-independent."""
    return hashlib.sha256(("".join(sorted(digests)) + schema_hash_value).encode()).hexdigest()
