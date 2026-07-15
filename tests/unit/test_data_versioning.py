# tests/unit/test_data_versioning.py
"""A1 — Data & dataset versioning (ADR 0003, spec A1-data-versioning).

Covers the spec's Given-When-Then acceptance criteria:
  GWT-1 determinism · GWT-3 idempotency · GWT-4 fail-open · GWT-5 diff.
(GWT-2 pinning is exercised at the CLI level in test_cli_data.py.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.platform_db import (  # noqa: E402
    get_dataset_revision,
    get_dataset_revisions,
    init_db,
    record_dataset_revision,
)
from pipelines.datasets.versioning import (  # noqa: E402
    UNKNOWN,
    DatasetRevision,
    content_revision,
    resolve_revision,
)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_LAKEFS_ENDPOINT", raising=False)
    init_db()


def _write_parquet(path: Path, cols: dict) -> Path:
    pq.write_table(pa.table(cols), path)
    return path


# --- content_revision core ---------------------------------------------------


def test_gwt1_determinism(tmp_path):
    """GWT-1: identical data ⇒ identical content revision id across runs."""
    p = _write_parquet(tmp_path / "a.parquet", {"x": [1, 2, 3], "y": [4.0, 5.0, 6.0]})
    rev1, sch1, _ = content_revision([p])
    rev2, sch2, _ = content_revision([p])
    assert rev1 == rev2
    assert sch1 == sch2
    assert len(rev1) == 64  # sha256 hex


def test_content_revision_order_independent(tmp_path):
    a = _write_parquet(tmp_path / "a.parquet", {"x": [1]})
    b = _write_parquet(tmp_path / "b.parquet", {"x": [2]})
    assert content_revision([a, b])[0] == content_revision([b, a])[0]


def test_content_revision_changes_with_data(tmp_path):
    a = _write_parquet(tmp_path / "a.parquet", {"x": [1, 2, 3]})
    b = _write_parquet(tmp_path / "b.parquet", {"x": [1, 2, 4]})
    assert content_revision([a])[0] != content_revision([b])[0]


def test_content_revision_changes_with_schema(tmp_path):
    a = _write_parquet(tmp_path / "a.parquet", {"x": [1, 2, 3]})
    b = _write_parquet(tmp_path / "b.parquet", {"z": [1, 2, 3]})
    ra, sa, _ = content_revision([a])
    rb, sb, _ = content_revision([b])
    assert sa != sb
    assert ra != rb


def test_content_revision_no_files_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        content_revision([tmp_path / "missing.parquet"])


# --- resolve_revision --------------------------------------------------------


def test_resolve_content_from_directory(tmp_path):
    _write_parquet(tmp_path / "part-0.parquet", {"x": [1, 2]})
    _write_parquet(tmp_path / "part-1.parquet", {"x": [3, 4]})
    rev = resolve_revision("minio", "FData", data_path=tmp_path)
    assert isinstance(rev, DatasetRevision)
    assert rev.kind == "content"
    assert rev.backend == "minio"
    assert rev.dataset == "FData"
    assert rev.is_known
    assert rev.byte_count and rev.byte_count > 0


def test_resolve_legacy_backend_name(tmp_path):
    p = _write_parquet(tmp_path / "a.parquet", {"x": [1]})
    rev = resolve_revision(None, "FData", data_path=p)
    assert rev.backend == "legacy"


def test_gwt4_fail_open(tmp_path):
    """GWT-4: unresolvable data ⇒ 'unknown' revision, never an exception."""
    rev = resolve_revision("minio", "FData", data_path=tmp_path / "nope")
    assert rev.revision_id == UNKNOWN
    assert rev.kind == UNKNOWN
    assert not rev.is_known


def test_gwt4_fail_open_no_path(tmp_path):
    rev = resolve_revision("minio", "FData")
    assert rev.revision_id == UNKNOWN


def test_resolve_lakefs_preferred(tmp_path, monkeypatch):
    """When lakeFS resolves, kind is 'lakefs' and content path is skipped."""
    from pipelines.datasets import versioning

    monkeypatch.setenv("EXAMLOPS_LAKEFS_ENDPOINT", "http://lakefs:8000")
    sentinel = DatasetRevision(
        backend="minio", dataset="FData", revision_id="commit-abc", kind="lakefs"
    )
    monkeypatch.setattr(versioning, "_lakefs_revision", lambda b, d: sentinel)
    rev = resolve_revision("minio", "FData", data_path=tmp_path)
    assert rev.kind == "lakefs"
    assert rev.revision_id == "commit-abc"


# --- platform_db persistence -------------------------------------------------


def test_record_and_get(tmp_path):
    p = _write_parquet(tmp_path / "a.parquet", {"x": [1, 2, 3]})
    rev = resolve_revision("minio", "FData", data_path=p)
    record_dataset_revision(rev, mlflow_run_id="run1", row_count=3, byte_count=99, actor="me")
    rows = get_dataset_revisions("FData")
    assert len(rows) == 1
    assert rows[0]["revision_id"] == rev.revision_id
    assert rows[0]["mlflow_run_id"] == "run1"
    assert rows[0]["actor"] == "me"
    assert rows[0]["kind"] == "content"


def test_gwt3_idempotency(tmp_path):
    """GWT-3: recording the same revision twice ⇒ one row."""
    p = _write_parquet(tmp_path / "a.parquet", {"x": [1]})
    rev = resolve_revision("minio", "FData", data_path=p)
    record_dataset_revision(rev, row_count=1, byte_count=1, actor="me")
    record_dataset_revision(rev, row_count=1, byte_count=1, actor="me")
    assert len(get_dataset_revisions("FData")) == 1


def test_get_filters_by_backend(tmp_path):
    p = _write_parquet(tmp_path / "a.parquet", {"x": [1]})
    r_minio = resolve_revision("minio", "FData", data_path=p)
    r_zenodo = DatasetRevision(backend="zenodo", dataset="FData", revision_id="zzz", kind="content")
    record_dataset_revision(r_minio, actor="me")
    record_dataset_revision(r_zenodo, actor="me")
    assert len(get_dataset_revisions("FData")) == 2
    assert len(get_dataset_revisions("FData", backend="minio")) == 1
    assert get_dataset_revisions("FData", backend="minio")[0]["revision_id"] == r_minio.revision_id


def test_get_single_revision(tmp_path):
    p = _write_parquet(tmp_path / "a.parquet", {"x": [1]})
    rev = resolve_revision("minio", "FData", data_path=p)
    record_dataset_revision(rev, actor="me")
    assert get_dataset_revision("FData", rev.revision_id) is not None
    assert get_dataset_revision("FData", "does-not-exist") is None


def test_newest_first(tmp_path):
    for i in range(3):
        rev = DatasetRevision(
            backend="minio", dataset="FData", revision_id=f"rev{i}", kind="content"
        )
        record_dataset_revision(rev, actor="me")
    ids = [r["revision_id"] for r in get_dataset_revisions("FData")]
    assert ids == ["rev2", "rev1", "rev0"]
