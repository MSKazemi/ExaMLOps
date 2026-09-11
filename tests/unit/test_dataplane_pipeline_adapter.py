"""ADR 0130 §8 — resolve once, pin once: bindings, memoized pins, fetch() mapping."""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.dataplane import store as st  # noqa: E402
from examlops.dataplane.types import Limits, TableBatch  # noqa: E402
from pipelines.datasets import dataplane as ad  # noqa: E402


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    url = f"file://{tmp_path / 'store'}"
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", url)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_CACHE_DIR", str(tmp_path / "cache"))
    # A pinned revision leaked by an earlier test on this worker would redirect `pin_for` to a
    # snapshot that does not exist in this test's store.
    monkeypatch.delenv("EXAMLOPS_DATASET_REVISION", raising=False)
    ad.reset_pins()
    store = st.DatasetStore.from_url(url)

    def commit(values, pull_id, parent=None):
        w = st.SnapshotWriter(tmp_path / pull_id, limits=Limits())
        w.write(TableBatch("job_table", pa.RecordBatch.from_pylist([{"v": v} for v in values])))
        return st.publish(
            store,
            "_global/pm100",
            staged=w.close(),
            parent=parent,
            connector="sql",
            connection=None,
            spec_hash="h",
            watermark={},
            pull_id=pull_id,
            incremental=False,
        )[0]

    yield commit
    ad.reset_pins()


def test_binding_parsing():
    assert ad.DataplaneBinding.from_yaml(None) is None
    b = ad.DataplaneBinding.from_yaml(
        {"source": "pm100", "tables": {"PM100/job_table.parquet": "job_table"}}
    )
    assert b.revision == "latest" and b.tables["PM100/job_table.parquet"] == "job_table"
    with pytest.raises(ValueError, match="source"):
        ad.DataplaneBinding.from_yaml({"revision": "latest"})


def test_pin_is_resolved_once_per_run(snapshot):
    first = snapshot([1, 2], "p1")
    binding = ad.DataplaneBinding(source="pm100")
    pin = ad.pin_for("JPCP", "PM100Dataset", binding)
    assert pin.revision == first.revision
    snapshot([1, 2, 3], "p2", parent=first)  # latest moves mid-run
    assert ad.pin_for("JPCP", "PM100Dataset", binding).revision == first.revision
    assert ad.current_pin("jpcp", "PM100Dataset") == pin


def test_env_revision_pins_an_older_snapshot(snapshot, monkeypatch):
    first = snapshot([1], "p1")
    snapshot([1, 2], "p2", parent=first)
    monkeypatch.setenv("EXAMLOPS_DATASET_REVISION", first.revision)
    assert (
        ad.pin_for("JPCP", "PM100Dataset", ad.DataplaneBinding(source="pm100")).revision
        == first.revision
    )


def test_fetch_maps_logical_paths_and_fails_loudly(snapshot, tmp_path):
    snapshot([1, 2], "p1")
    pin = ad.pin_for("JPCP", "PM100Dataset", ad.DataplaneBinding(source="pm100"))
    backend = ad.DataplaneDatasetBackend(pin, {})
    path = backend.fetch("PM100/job_table.parquet", tmp_path)
    assert Path(path).exists()
    with pytest.raises(ad.LogicalPathNotInSnapshot, match="job_table"):
        backend.fetch("FData/24_02.parquet", tmp_path)


def test_backend_satisfies_the_modelzoo_protocol_structurally(snapshot):
    snapshot([1], "p1")
    pin = ad.pin_for("JPCP", "PM100Dataset", ad.DataplaneBinding(source="pm100"))
    backend = ad.DataplaneDatasetBackend(pin, {})
    assert backend.name == "dataplane" and callable(backend.fetch)


def test_cache_root_follows_the_data_root_not_the_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_CACHE_DIR", raising=False)
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "data"))
    assert ad.cache_root() == tmp_path / "data" / "cache" / "dataplane"
    monkeypatch.delenv("EXAMLOPS_DATA_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ad.cache_root() == tmp_path / "xdg" / "examlops" / "dataplane"
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_CACHE_DIR", str(tmp_path / "explicit"))
    assert ad.cache_root() == tmp_path / "explicit"
