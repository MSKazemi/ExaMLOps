"""ADR 0130 §7 — snapshot store: atomic commit, content addressing, incremental, materialize, prune."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pyarrow as pa
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.dataplane import store as st  # noqa: E402
from examlops.dataplane.types import (  # noqa: E402
    LimitExceeded,
    Limits,
    SnapshotNotFound,
    TableBatch,
)
from pipelines.datasets.versioning import resolve_revision  # noqa: E402


@pytest.fixture(params=["fsspec-local", "arrow-wrapper"])
def store(request, tmp_path):
    if request.param == "arrow-wrapper":
        # Task 22b: S3 reaches the store as pyarrow's native filesystem behind fsspec's
        # ArrowFSWrapper — run every store test over that wrapper too (a local root, no live S3).
        from fsspec.implementations.arrow import ArrowFSWrapper
        from pyarrow.fs import LocalFileSystem

        root = tmp_path / "store"
        fs = ArrowFSWrapper(LocalFileSystem(), skip_instance_cache=True)
        return st.DatasetStore(fs, str(root), uri_prefix=f"file://{root}")
    return st.DatasetStore.from_url(f"file://{tmp_path / 'store'}")


def _batch(values, table="jobs"):
    return TableBatch(
        table,
        pa.RecordBatch.from_pylist([{"id": v, "power": v * 1.5} for v in values]),
        {"column": "id", "value": max(values), "type": "int"},
    )


def _pull(
    store,
    tmp_path,
    values,
    *,
    key="_global/pm100",
    parent=None,
    pull_id="p1",
    incremental=False,
    limits=Limits(),
):
    w = st.SnapshotWriter(tmp_path / f"stage-{pull_id}", limits=limits)
    for chunk in values:
        w.write(_batch(chunk))
    staged = w.close()
    return st.publish(
        store,
        key,
        staged=staged,
        parent=parent,
        connector="sql",
        connection="lab",
        spec_hash="h",
        watermark=w.watermark,
        pull_id=pull_id,
        incremental=incremental,
    )


def test_commit_writes_latest_last_and_resolves(store, tmp_path):
    manifest, changed = _pull(store, tmp_path, [[1, 2], [3]])
    assert changed and manifest.row_count == 3 and manifest.tables == ("jobs",)
    ref = st.resolve(store, "_global/pm100")
    assert ref.revision == manifest.revision
    assert st.resolve(store, "_global/pm100", manifest.revision).pull_id == "p1"


def test_uncommitted_pull_is_invisible(store, tmp_path, monkeypatch):
    real = st.DatasetStore.write_text

    def crash_on_latest(self, key, text):
        if key.endswith("/_latest"):
            raise RuntimeError("crash before commit")
        return real(self, key, text)

    monkeypatch.setattr(st.DatasetStore, "write_text", crash_on_latest)
    with pytest.raises(RuntimeError):
        _pull(store, tmp_path, [[1]])
    monkeypatch.undo()
    with pytest.raises(SnapshotNotFound):
        st.resolve(store, "_global/pm100")


def test_identical_data_is_unchanged(store, tmp_path):
    first, _ = _pull(store, tmp_path, [[1, 2]], pull_id="p1")
    second, changed = _pull(store, tmp_path, [[1, 2]], parent=first, pull_id="p2")
    assert changed is False and second.revision == first.revision


def test_incremental_carries_parent_files(store, tmp_path):
    first, _ = _pull(store, tmp_path, [[1, 2]], pull_id="p1")
    second, changed = _pull(store, tmp_path, [[3]], parent=first, pull_id="p2", incremental=True)
    assert changed and second.row_count == 3 and second.parent_revision == first.revision
    assert {f.path for f in second.files} == {"jobs/part-00000.parquet", "jobs/part-00001.parquet"}


def test_materialized_copy_rehashes_to_the_pinned_revision(store, tmp_path):
    manifest, _ = _pull(store, tmp_path, [[1, 2], [3]])
    ref = st.resolve(store, "_global/pm100")
    local = st.materialize(store, ref, tmp_path / "cache")
    assert resolve_revision("dataplane", "pm100", data_path=local).revision_id == manifest.revision
    assert st.materialize(store, ref, tmp_path / "cache") == local  # cached


def test_row_limit_is_enforced(tmp_path):
    w = st.SnapshotWriter(tmp_path / "s", limits=Limits(max_rows=2))
    with pytest.raises(LimitExceeded, match="max_rows"):
        w.write(_batch([1, 2, 3]))


def test_schema_change_rolls_a_new_part(tmp_path):
    w = st.SnapshotWriter(tmp_path / "s", limits=Limits())
    w.write(TableBatch("t", pa.RecordBatch.from_pylist([{"a": 1}])))
    w.write(TableBatch("t", pa.RecordBatch.from_pylist([{"a": "x"}])))
    assert [rel for rel, _ in w.close()] == ["t/part-00000.parquet", "t/part-00001.parquet"]


def test_prune_keeps_latest_and_pinned(store, tmp_path):
    m1, _ = _pull(store, tmp_path, [[1]], pull_id="p1")
    m2, _ = _pull(store, tmp_path, [[2]], parent=m1, pull_id="p2")
    m3, _ = _pull(store, tmp_path, [[3]], parent=m2, pull_id="p3")
    removed = st.prune(store, "_global/pm100", keep=1, pinned={m1.revision})
    assert removed == ["p2"]
    st.resolve(store, "_global/pm100", m1.revision)
    with pytest.raises(SnapshotNotFound):
        st.resolve(store, "_global/pm100", m2.revision)


def test_prune_rewrites_revision_pointer_shared_with_a_kept_pull(store, tmp_path):
    # publish() only dedups against the *immediate* parent, so a non-adjacent pull can land back
    # on a revision a kept pull still carries: A (p1) -> B (p2) -> A again (p3).
    m1, _ = _pull(store, tmp_path, [[1]], pull_id="p1")
    m2, _ = _pull(store, tmp_path, [[2]], parent=m1, pull_id="p2")
    m3, _ = _pull(store, tmp_path, [[1]], parent=m2, pull_id="p3")
    assert m3.revision == m1.revision != m2.revision

    removed = st.prune(store, "_global/pm100", keep=1, pinned=set())
    assert set(removed) == {"p1", "p2"}  # only p3 (_latest) is kept

    # revision A must still resolve — its pointer must have been rewritten to the kept pull p3,
    # not deleted just because the pull that originally wrote it (p1) was removed.
    ref = st.resolve(store, "_global/pm100", m1.revision)
    assert ref.pull_id == "p3"
    with pytest.raises(SnapshotNotFound):
        st.resolve(store, "_global/pm100", m2.revision)


def test_prune_grace_protects_young_manifested_pull_and_removes_old_one(store, tmp_path):
    from examlops.data.dataplane import new_pull_id

    young_id = new_pull_id()
    m1, _ = _pull(store, tmp_path, [[1]], pull_id=young_id)

    old_ns = time.time_ns() - (st._ORPHAN_AGE_S + 60) * 1_000_000_000
    old_id = f"{old_ns:016x}abcdef"
    m_old, _ = _pull(store, tmp_path, [[2]], parent=m1, pull_id=old_id)

    latest_id = new_pull_id()
    _pull(store, tmp_path, [[3]], parent=m_old, pull_id=latest_id)

    # The grace counts from a pull dir's newest object (fix I4c), so the old pull's objects must
    # be old too — a pull that started long ago but is uploading now is not garbage.
    then = time.time() - (st._ORPHAN_AGE_S + 60)
    for path in (Path(store.root) / "_global" / "pm100" / old_id).rglob("*"):
        os.utime(path, (then, then))

    removed = st.prune(store, "_global/pm100", keep=0, pinned=set())
    assert old_id in removed
    assert young_id not in removed

    # the young pull's manifest and revision pointer must both have survived the age grace.
    ref = st.resolve(store, "_global/pm100", m1.revision)
    assert ref.pull_id == young_id


def test_materialize_is_safe_under_concurrent_callers(store, tmp_path):
    manifest, _ = _pull(store, tmp_path, [[1, 2], [3]])
    ref = st.resolve(store, "_global/pm100")
    cache_root = tmp_path / "cache"
    results: list[Path] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            path = st.materialize(store, ref, cache_root)
        except BaseException as exc:  # noqa: BLE001 - surfaced via the errors list, not raised here
            with lock:
                errors.append(exc)
        else:
            with lock:
                results.append(path)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 8
    assert len(set(results)) == 1
    local = results[0]
    assert resolve_revision("dataplane", "pm100", data_path=local).revision_id == manifest.revision
