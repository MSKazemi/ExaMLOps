"""A1 — Data & dataset versioning (ADR 0003, spec ``A1-data-versioning``).

Resolves any dataset to an immutable :class:`DatasetRevision` before training so a
re-run can be pinned to the exact data it used. Two strategies behind one value
object:

* **lakeFS** (preferred) — when ``EXAMLOPS_LAKEFS_ENDPOINT`` is set, the revision is
  a lakeFS commit id (``kind="lakefs"``).
* **content-hash fallback** — otherwise the revision is a deterministic SHA-256 over
  the resolved parquet file digests plus the schema (``kind="content"``). This keeps
  the feature fully functional with no infra dependency (laptop, CI, tests).

Resolution is **fail-open** (spec R4): any error yields ``revision_id="unknown"`` and
a logged warning rather than failing a pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"
_CHUNK = 1 << 20  # 1 MiB streaming read — never load a multi-GB parquet into memory.


@dataclass(frozen=True)
class DatasetRevision:
    """Immutable identity of a dataset state (spec §2)."""

    backend: str
    dataset: str
    revision_id: str
    kind: str  # "lakefs" | "content" | "unknown"
    uri: str = ""
    schema_hash: str = ""
    created_at: str = ""
    row_count: int | None = None
    byte_count: int | None = None

    @property
    def is_known(self) -> bool:
        return self.revision_id != UNKNOWN


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _file_digest(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _schema_hash(paths: list[Path]) -> str:
    """Deterministic hash of the parquet schema across ``paths``.

    Uses pyarrow when available; folds ``name:type`` pairs in column order. Returns
    ``""`` when the schema cannot be read (non-parquet, pyarrow absent) — the file
    digests still make the revision id unique, so this only weakens the *diff* facet.
    """
    try:
        import pyarrow.parquet as pq
    except Exception:  # pragma: no cover - pyarrow is a hard dep here, defensive only
        return ""
    parts: list[str] = []
    for path in paths:
        try:
            schema = pq.read_schema(path)
        except Exception:
            continue
        parts.append("|".join(f"{name}:{schema.field(name).type}" for name in schema.names))
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def content_revision(paths: Iterable[Path]) -> tuple[str, str, int]:
    """Pure content-hash core (spec R3).

    Returns ``(revision_id, schema_hash, byte_count)`` for the given file set. The
    ``revision_id`` is ``sha256(sorted file digests ‖ schema_hash)`` and is therefore
    deterministic across runs on identical data (spec GWT-1) and order-independent.

    Raises ``FileNotFoundError`` if no readable files are supplied — callers wrap this
    into the fail-open path.
    """
    resolved = sorted(Path(p) for p in paths)
    existing = [p for p in resolved if p.is_file()]
    if not existing:
        raise FileNotFoundError("no readable files to hash for content revision")
    digests = sorted(_file_digest(p) for p in existing)
    schema_hash = _schema_hash(existing)
    byte_count = sum(p.stat().st_size for p in existing)
    revision_id = hashlib.sha256(("".join(digests) + schema_hash).encode()).hexdigest()
    return revision_id, schema_hash, byte_count


def count_rows(paths: Iterable[Path]) -> int | None:
    """Total parquet row count across ``paths`` via footer metadata (no data read).

    Returns None when the count cannot be determined (pyarrow absent / unreadable).
    """
    try:
        import pyarrow.parquet as pq
    except Exception:  # pragma: no cover - defensive
        return None
    total = 0
    seen = False
    for p in paths:
        path = Path(p)
        if not path.is_file():
            continue
        try:
            total += pq.read_metadata(path).num_rows
            seen = True
        except Exception:
            continue
    return total if seen else None


def discover_files(candidate: str | os.PathLike[str] | None) -> list[Path]:
    """Expand a file or directory into the parquet file set to hash."""
    if candidate is None:
        return []
    path = Path(candidate)
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.parquet"))
    return []


# Backwards-compatible private alias (used internally before it was made public).
_discover_files = discover_files


def _lakefs_revision(backend: str, dataset: str) -> DatasetRevision | None:
    """Best-effort lakeFS commit-id resolution.

    Only attempted when ``EXAMLOPS_LAKEFS_ENDPOINT`` is set. Any failure returns None
    so the caller falls through to the content-hash strategy (spec R2 + fail-open).
    """
    endpoint = os.getenv("EXAMLOPS_LAKEFS_ENDPOINT", "").strip()
    if not endpoint:
        return None
    repo = os.getenv("EXAMLOPS_LAKEFS_REPO", dataset)
    ref = os.getenv("EXAMLOPS_LAKEFS_REF", "main")
    try:
        import urllib.request

        url = f"{endpoint.rstrip('/')}/api/v1/repositories/{repo}/refs/{ref}/commits"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - operator-configured
            import json

            data = json.loads(resp.read().decode())
        commit_id = (data.get("results") or [{}])[0].get("id")
        if not commit_id:
            return None
        return DatasetRevision(
            backend=backend,
            dataset=dataset,
            revision_id=str(commit_id),
            kind="lakefs",
            uri=f"lakefs://{repo}/{commit_id}",
            created_at=_now(),
        )
    except Exception as exc:  # pragma: no cover - network path, exercised via monkeypatch
        logger.warning("lakeFS revision resolution failed for %s/%s: %s", backend, dataset, exc)
        return None


def resolve_revision(
    backend: str | None,
    dataset: str,
    *,
    data_path: str | os.PathLike[str] | None = None,
) -> DatasetRevision:
    """Resolve ``dataset`` to a concrete :class:`DatasetRevision` (spec R1-R4).

    ``backend`` is the storage backend name (``zenodo``/``minio``/``dataplane`` or
    ``None`` for legacy). ``data_path`` optionally points at the already-materialised
    file or directory to hash; when omitted only the lakeFS path can produce a known
    revision and the content path fails open to ``unknown``.

    Never raises: on any error the revision is ``unknown`` with a warning.
    """
    backend_name = backend or "legacy"

    lake = _lakefs_revision(backend_name, dataset)
    if lake is not None:
        return lake

    try:
        files = _discover_files(data_path)
        revision_id, schema_hash, byte_count = content_revision(files)
        return DatasetRevision(
            backend=backend_name,
            dataset=dataset,
            revision_id=revision_id,
            kind="content",
            uri=str(Path(data_path)) if data_path else "",
            schema_hash=schema_hash,
            created_at=_now(),
            byte_count=byte_count,
        )
    except Exception as exc:
        logger.warning(
            "dataset revision resolution failed for %s/%s (fail-open to 'unknown'): %s",
            backend_name,
            dataset,
            exc,
        )
        return DatasetRevision(
            backend=backend_name,
            dataset=dataset,
            revision_id=UNKNOWN,
            kind=UNKNOWN,
            created_at=_now(),
        )
