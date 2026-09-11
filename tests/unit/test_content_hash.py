"""ADR 0130 — the dataplane and dataset versioning share one content-hash formula."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.data.content_hash import (  # noqa: E402
    file_digest,
    revision_id,
    schema_hash,
    schema_part,
)
from pipelines.datasets.versioning import content_revision  # noqa: E402


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def _legacy_revision(paths: list[Path]) -> str:
    """The formula as it stood before extraction (versioning.py, 2026-09-10)."""
    existing = sorted(paths)
    digests = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in existing)
    parts = []
    for p in existing:
        s = pq.read_schema(p)
        parts.append("|".join(f"{n}:{s.field(n).type}" for n in s.names))
    sh = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return hashlib.sha256(("".join(digests) + sh).encode()).hexdigest()


def test_extracted_formula_matches_the_legacy_one(tmp_path):
    a = _write(tmp_path / "t" / "part-00000.parquet", [{"x": 1, "y": "a"}])
    b = _write(tmp_path / "t" / "part-00001.parquet", [{"x": 2, "y": "b"}])
    paths = sorted([a, b])
    rev = revision_id([file_digest(p) for p in paths], schema_hash(schema_part(p) for p in paths))
    assert rev == _legacy_revision(paths)
    assert content_revision(paths)[0] == rev


def test_schema_hash_ignores_unreadable_files(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not parquet")
    assert schema_part(bad) is None
    assert schema_hash([None, None]) == ""


def test_revision_is_order_independent(tmp_path):
    a = _write(tmp_path / "a.parquet", [{"x": 1}])
    b = _write(tmp_path / "b.parquet", [{"x": 2}])
    d = [file_digest(a), file_digest(b)]
    assert revision_id(d, "s") == revision_id(list(reversed(d)), "s")
