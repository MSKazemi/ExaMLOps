"""Unit tests for the Phase 1 dataset backend abstraction.

These tests validate the backend layer in isolation — no Zenodo, MinIO, or
dataplane network calls. Network-touching paths are exercised via mocks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELZOO = REPO_ROOT / "modelzoo"
for p in (str(REPO_ROOT), str(MODELZOO)):
    if p not in sys.path:
        sys.path.insert(0, p)

# ── upstream-library guard ───────────────────────────────────────────────────
# `seanergys_modelzoo` is an UPSTREAM library, not part of ExaMLOps (ADR 0094):
# the platform core never imports it — only the use-case pack does, through the
# loader seam. It is therefore not vendored in the public tree; CI and the
# deploy node fetch it from its own repo. Skip rather than fail when absent.
_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (REPO_ROOT / "modelzoo"))
if not (_MZ / "seanergys_modelzoo").is_dir():
    pytest.skip(
        "seanergys_modelzoo not present — upstream library fetched at deploy/CI "
        "time. Set EXAMLOPS_MODELZOO_DIR to a checkout to run these tests.",
        allow_module_level=True,
    )
if str(_MZ) not in sys.path:
    sys.path.insert(0, str(_MZ))


from seanergys_modelzoo.datasets._backends import (  # noqa: E402
    DataplaneBackend,
    DatasetBackend,
    MinIOBackend,
    ZenodoBackend,
    get_backend,
)

# ── ZenodoBackend ─────────────────────────────────────────────────────────────


class TestZenodoBackend:
    def test_resolves_pm100_to_public_url(self, tmp_path):
        backend = ZenodoBackend()
        path = backend.fetch("PM100/job_table.parquet", tmp_path)
        assert str(path).startswith("https://zenodo.org/records/10127767/files/")
        assert str(path).endswith("job_table.parquet")

    def test_resolves_fdata_per_file(self, tmp_path):
        backend = ZenodoBackend()
        path = backend.fetch("FData/21_03.parquet", tmp_path)
        assert "11467483" in str(path)
        assert str(path).endswith("21_03.parquet")

    def test_unknown_dataset_raises(self, tmp_path):
        with pytest.raises(KeyError):
            ZenodoBackend().fetch("NotARealDataset/file.parquet", tmp_path)

    def test_malformed_logical_path_raises(self, tmp_path):
        with pytest.raises(ValueError):
            ZenodoBackend().fetch("noslash", tmp_path)


# ── MinIOBackend (with mocked boto3) ──────────────────────────────────────────


class TestMinIOBackend:
    def test_caches_to_local_path(self, tmp_path, monkeypatch):
        downloads = []

        class FakeS3:
            def download_file(self, bucket, key, dest):
                downloads.append((bucket, key, dest))
                Path(dest).write_bytes(b"PARQUET")

        backend = MinIOBackend(
            bucket="examlops-data",
            endpoint_url="http://localhost:9000",
            access_key="x",
            secret_key="y",
        )
        backend._client = FakeS3()  # bypass boto3 import

        path = backend.fetch("PM100/job_table.parquet", tmp_path)
        assert path.exists()
        assert path.read_bytes() == b"PARQUET"
        assert downloads == [("examlops-data", "PM100/job_table.parquet", str(path))]

    def test_skips_download_if_cached(self, tmp_path):
        backend = MinIOBackend(bucket="examlops-data")
        # Pre-seed the cache.
        cached = tmp_path / "PM100_job_table.parquet"
        cached.write_bytes(b"CACHED")

        class FailingClient:
            def download_file(self, *args, **kwargs):
                pytest.fail("download_file should not be called when cache hit")

        backend._client = FailingClient()
        path = backend.fetch("PM100/job_table.parquet", tmp_path)
        assert path == cached


# ── DataplaneBackend (with mocked HTTP) ───────────────────────────────────────


class TestDataplaneBackend:
    def test_writes_parquet_snapshot(self, tmp_path, monkeypatch):
        sample = [
            {"job_id": 1, "embedding": [0.1, 0.2], "pclass": "memory-bound"},
            {"job_id": 2, "embedding": [0.3, 0.4], "pclass": "compute-bound"},
        ]

        backend = DataplaneBackend(base_url="http://dataplane", snapshot_ttl_seconds=0)
        monkeypatch.setattr(backend, "_poll_all_jobs", lambda: sample)

        path = backend.fetch("FData/snapshot.parquet", tmp_path)
        assert path.exists()

        import pandas as pd

        df = pd.read_parquet(path)
        assert len(df) == 2
        assert list(df["pclass"]) == ["memory-bound", "compute-bound"]

    def test_empty_payload_raises(self, tmp_path, monkeypatch):
        backend = DataplaneBackend(base_url="http://dataplane")
        monkeypatch.setattr(backend, "_poll_all_jobs", lambda: [])
        with pytest.raises(RuntimeError, match="no rows returned"):
            backend.fetch("FData/snapshot.parquet", tmp_path)


# ── Factory ───────────────────────────────────────────────────────────────────


class TestGetBackend:
    def test_none_returns_none(self):
        assert get_backend(None) is None
        assert get_backend("") is None
        assert get_backend("none") is None

    def test_zenodo_returns_zenodo_instance(self):
        backend = get_backend("zenodo")
        assert isinstance(backend, ZenodoBackend)
        assert isinstance(backend, DatasetBackend)

    def test_minio_returns_minio_instance(self):
        backend = get_backend("minio")
        assert isinstance(backend, MinIOBackend)

    def test_dataplane_returns_dataplane_instance(self):
        backend = get_backend("dataplane")
        assert isinstance(backend, DataplaneBackend)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown dataset backend"):
            get_backend("not-a-backend")
