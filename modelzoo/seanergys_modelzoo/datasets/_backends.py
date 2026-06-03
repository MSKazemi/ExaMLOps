"""
Dataset backends — pluggable storage layer for SeanergysDataset.

A backend resolves a logical dataset name (e.g. ``"PM100/job_table.parquet"``
or ``"FData/21_03.parquet"``) to a local parquet-readable path. The dataset
classes (PM100Dataset, FDataDataset, ScriptAIDataset) consult a backend
instead of hard-coding a Zenodo URL, which makes the platform work uniformly
against three sources:

* :class:`ZenodoBackend`   — public Zenodo records (default; matches legacy behaviour).
* :class:`MinIOBackend`    — S3-compatible object store (boto3).
* :class:`DataplaneBackend` — REST API of the EU-project HPC dataplane (or its
  simulator at ``clients/dataplane_sim.py``).

Selection at runtime
--------------------
Pipelines and configs pass a ``backend_name`` (``"zenodo"`` / ``"minio"`` /
``"dataplane"``). :func:`get_backend` returns a configured instance; env vars
populate credentials so dev and prod look the same to user code.

The ``backend`` attribute on a dataset is optional. When ``None``, the legacy
Zenodo URL flow is preserved for backward compatibility.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

# Backends may return either a local Path or a remote URL string. Datasets call
# str(...) on the result and pass it to pandas.read_parquet, which handles
# both transparently. Path-wrapping URLs collapses double slashes — never do that.
PathOrURL = Path | str


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class DatasetBackend(Protocol):
    """Resolves a logical dataset path to a local parquet-readable file/dir.

    Implementations MUST be idempotent: a second ``fetch`` call with the same
    logical path should hit the cache (no re-download) when possible.
    """

    name: str

    def fetch(self, logical_path: str, cache_dir: Path) -> PathOrURL:
        """Return a path or URL that ``pandas.read_parquet`` can open.

        Args:
            logical_path: e.g. ``"PM100/job_table.parquet"``,
                ``"FData/21_03.parquet"``, ``"ScriptAI/script_ai.parquet"``.
            cache_dir: Local directory where the backend MAY cache results.

        Returns:
            Either a local :class:`Path` (preferred — enables filtered reads)
            or a remote URL :class:`str` that pandas can fetch directly.
        """
        ...


# ── Zenodo (default, mirrors existing behaviour) ──────────────────────────────


class ZenodoBackend:
    """Resolves dataset paths to public Zenodo download URLs.

    No credentials. ``pandas.read_parquet`` follows the URL directly; the
    dataset classes handle local caching.
    """

    name = "zenodo"

    # logical path prefix → record URL prefix on Zenodo.
    _RECORDS = {
        "PM100": "https://zenodo.org/records/10127767/files",
        "FData": "https://zenodo.org/records/11467483/files",
        "ScriptAI": "https://zenodo.org/records/14794193/files",
    }

    def fetch(self, logical_path: str, cache_dir: Path) -> str:
        if "/" not in logical_path:
            raise ValueError(
                f"ZenodoBackend: logical_path must be '<dataset>/<file>', got {logical_path!r}"
            )
        prefix, file_name = logical_path.split("/", 1)

        record = self._RECORDS.get(prefix)
        if record is None:
            raise KeyError(
                f"ZenodoBackend: no Zenodo record registered for {prefix!r}; "
                f"known: {sorted(self._RECORDS)}"
            )

        # Return the raw URL as a string — Path() would collapse "https://" into
        # "https:/". Callers that need local caching do it themselves
        # (PM100Dataset / FDataDataset already cache full files).
        return f"{record}/{file_name}"


# ── MinIO / S3 ────────────────────────────────────────────────────────────────


class MinIOBackend:
    """S3-compatible object store backend (MinIO, AWS S3, Wasabi, …).

    Looks up ``s3://{bucket}/{logical_path}``, downloads to ``cache_dir``
    if not already present, and returns the local path. Bucket layout
    convention: ``s3://{bucket}/{dataset_name}/{version_or_file}.parquet``.
    """

    name = "minio"

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000")
        self.access_key = access_key or os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
        self.secret_key = secret_key or os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
        self.region = region
        self._client = None  # lazy

    def _s3(self):
        if self._client is None:
            import boto3  # local import keeps modelzoo importable without boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
            )
        return self._client

    def fetch(self, logical_path: str, cache_dir: Path) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        local_path = cache_dir / logical_path.replace("/", "_")
        if local_path.exists():
            return local_path

        self._s3().download_file(self.bucket, logical_path, str(local_path))
        return local_path


# ── Dataplane (HPC dataplane REST API or simulator) ───────────────────────────


class DataplaneBackend:
    """Polls the HPC dataplane REST API and writes a local parquet snapshot.

    The simulator at ``clients/dataplane_sim.py`` exposes ``GET /jobs`` which
    returns a JSON array of job records. This backend pages through ``/jobs``,
    materialises a :class:`pandas.DataFrame`, writes it to ``cache_dir`` as a
    parquet file, and returns the path.

    Snapshots are time-stamped per ``logical_path`` so each pipeline run sees
    a fresh view; pass ``snapshot_ttl_seconds`` to reuse a recent snapshot.
    """

    name = "dataplane"

    def __init__(
        self,
        base_url: str | None = None,
        snapshot_ttl_seconds: int = 300,
        page_size: int = 1000,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("DATAPLANE_URL", "http://localhost:8010")).rstrip("/")
        self.snapshot_ttl_seconds = snapshot_ttl_seconds
        self.page_size = page_size
        self.timeout = timeout

    def fetch(self, logical_path: str, cache_dir: Path) -> Path:
        import time

        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{logical_path.replace('/', '_')}"

        # Reuse recent snapshot if fresh enough.
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < self.snapshot_ttl_seconds:
                return cache_file

        rows = self._poll_all_jobs()
        if not rows:
            raise RuntimeError(
                f"DataplaneBackend: no rows returned from {self.base_url}/jobs"
            )

        import pandas as pd

        df = pd.DataFrame(rows)
        df.to_parquet(cache_file, index=False)
        return cache_file

    def _poll_all_jobs(self):
        import json
        import urllib.parse
        import urllib.request

        url = f"{self.base_url}/jobs?limit={self.page_size}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        # Simulator returns either a list or {"jobs": [...]}; tolerate both.
        if isinstance(payload, dict):
            return payload.get("jobs") or payload.get("items") or []
        return payload


# ── Factory ──────────────────────────────────────────────────────────────────


def get_backend(name: str | None, **kwargs) -> DatasetBackend | None:
    """Return a configured backend by name, or ``None`` for legacy behaviour.

    ``name=None`` (or ``"none"``) preserves the legacy in-dataset URL handling,
    which is required for backward compatibility with existing configs and
    pipelines that pre-date the backend abstraction.
    """
    if name is None or name.lower() in ("", "none"):
        return None

    name = name.lower()
    if name == "zenodo":
        return ZenodoBackend()
    if name == "minio":
        bucket = kwargs.pop("bucket", None) or os.getenv("EXAMLOPS_DATA_BUCKET", "examlops-data")
        return MinIOBackend(bucket=bucket, **kwargs)
    if name == "dataplane":
        return DataplaneBackend(**kwargs)

    raise ValueError(
        f"Unknown dataset backend: {name!r}. Valid: zenodo, minio, dataplane."
    )


__all__ = [
    "DatasetBackend",
    "ZenodoBackend",
    "MinIOBackend",
    "DataplaneBackend",
    "get_backend",
]
