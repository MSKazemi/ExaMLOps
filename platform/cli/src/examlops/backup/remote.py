"""Off-site replication — push / list / pull / prune backup bundles on S3 (or MinIO).

A completed bundle directory is tar+gzipped and uploaded to ``EXAMLOPS_BACKUP_S3_URI``
(``s3://bucket/prefix``); its ``bundle.manifest.json`` is uploaded alongside (uncompressed) so a
remote ``list`` is cheap without pulling the archive. ``pull`` downloads + extracts a bundle; the
caller then runs the normal ``verify_bundle`` before any restore.

boto3 is optional (``examlops[backup]``) and lazily imported via the :func:`_s3_client` seam (shared
shape with :mod:`examlops.backup.objects_tier`); a missing dep or unreachable endpoint degrades to a
no-op / skip rather than crashing a scheduled cycle.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path
from typing import Any

from ._manifest import TierUnavailable


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// uri: {uri}")
    rest = uri[len("s3://") :]
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix.rstrip("/")


def _s3_client():  # noqa: ANN202
    """S3 client for off-site (own creds/endpoint, falling back to the MinIO ones). Test seam."""
    try:
        import boto3  # noqa: PLC0415
    except ImportError as exc:
        raise TierUnavailable("boto3 not installed — pip install 'examlops[backup]'") from exc
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("EXAMLOPS_BACKUP_S3_ENDPOINT")
        or os.getenv("MLFLOW_S3_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("EXAMLOPS_BACKUP_S3_ACCESS_KEY")
        or os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("EXAMLOPS_BACKUP_S3_SECRET")
        or os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
    )


def _tar_bundle(bundle_dir: Path, dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_dir.name)


def push(bundle_dir: str, *, s3_uri: str | None = None) -> dict[str, Any]:
    """Tar a bundle and upload it (+ its manifest) to the off-site prefix."""
    uri = s3_uri or os.getenv("EXAMLOPS_BACKUP_S3_URI", "")
    if not uri:
        raise TierUnavailable("no off-site target (EXAMLOPS_BACKUP_S3_URI unset)")
    bd = Path(bundle_dir)
    bucket, prefix = _parse_s3_uri(uri)
    s3 = _s3_client()
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"{bd.name}.tar.gz"
        _tar_bundle(bd, archive)
        base = f"{prefix}/{bd.name}" if prefix else bd.name
        s3.upload_file(str(archive), bucket, f"{base}.tar.gz")
        manifest = bd / "bundle.manifest.json"
        if manifest.exists():
            s3.upload_file(str(manifest), bucket, f"{base}.manifest.json")
    return {"pushed": bd.name, "uri": f"s3://{bucket}/{base}.tar.gz"}


def list_remote(*, s3_uri: str | None = None) -> list[dict[str, Any]]:
    """List off-site bundles by reading the sidecar manifests under the prefix."""
    import json

    uri = s3_uri or os.getenv("EXAMLOPS_BACKUP_S3_URI", "")
    if not uri:
        return []
    bucket, prefix = _parse_s3_uri(uri)
    s3 = _s3_client()
    out: list[dict[str, Any]] = []
    token = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            if not o["Key"].endswith(".manifest.json"):
                continue
            body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()
            m = json.loads(body)
            out.append(
                {
                    "bundle_id": m.get("bundle_id"),
                    "created_at": m.get("created_at"),
                    "overall_status": m.get("overall_status"),
                    "key": o["Key"].removesuffix(".manifest.json") + ".tar.gz",
                }
            )
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return sorted(out, key=lambda r: r.get("created_at") or "", reverse=True)


def pull(bundle_id: str, dest_dir: str, *, s3_uri: str | None = None) -> str:
    """Download + extract an off-site bundle into ``dest_dir``; returns the extracted bundle dir."""
    uri = s3_uri or os.getenv("EXAMLOPS_BACKUP_S3_URI", "")
    if not uri:
        raise TierUnavailable("no off-site target (EXAMLOPS_BACKUP_S3_URI unset)")
    bucket, prefix = _parse_s3_uri(uri)
    s3 = _s3_client()
    key = f"{prefix}/{bundle_id}.tar.gz" if prefix else f"{bundle_id}.tar.gz"
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"{bundle_id}.tar.gz"
        s3.download_file(bucket, key, str(archive))
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(dest, filter="data")  # noqa: S202 — our own bundle
    return str(dest / bundle_id)
