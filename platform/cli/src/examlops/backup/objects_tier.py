"""Object-storage backup tier — mirror MinIO/S3 buckets (MLflow artifacts + project storage).

Downloads every object of each configured bucket into ``objects/<bucket>/<key>`` and writes an
``_index.json`` (``key -> {size, etag, sha256}``). The tier checksum is a deterministic *tree hash*
over the sorted index, so ``verify`` can detect drift without re-downloading. Restore uploads the
indexed keys back (guarded behind ``--force``).

boto3 is an **optional** dependency (``examlops[backup]``), lazily imported here just like
``data/projects.py`` — if it is missing, or the endpoint is unreachable, the tier degrades to
``skipped``. Tests inject an in-memory fake via the :func:`_s3_client` seam, so no live MinIO is
needed. This tier is opt-in (``--with-objects``) precisely because a full artifact mirror can be
large and slow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ._manifest import OK, TierResult, TierUnavailable, rollup_status, sha256_file, sha256_text


def _buckets() -> list[str]:
    raw = os.getenv("EXAMLOPS_BACKUP_BUCKETS", "")
    if raw.strip():
        return [b.strip() for b in raw.split(",") if b.strip()]
    # Default: MLflow artifacts + the projects bucket (resolved the same way projects.py does).
    projects_bucket = os.getenv("EXAMLOPS_PROJECTS_BUCKET", "examlops-projects")
    return ["mlflow-artifacts", projects_bucket]


def _s3_client():  # noqa: ANN202 — returns a boto3 client or a test double
    """Build the S3 client. Seam for tests to monkeypatch with an in-memory fake."""
    try:
        import boto3  # noqa: PLC0415 — lazy: object tier is optional (examlops[backup])
    except ImportError as exc:
        raise TierUnavailable("boto3 not installed — pip install 'examlops[backup]'") from exc
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://localhost:19000"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
    )


def _list_keys(s3, bucket: str) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    token = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            keys.append({"Key": o["Key"], "Size": o.get("Size", 0), "ETag": o.get("ETag", "")})
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def backup_objects_tier(dest_dir: Path) -> TierResult:
    """Mirror each configured bucket into ``dest_dir/objects/<bucket>/`` + write per-bucket index."""
    try:
        s3 = _s3_client()
    except TierUnavailable:
        raise
    objects_root = dest_dir / "objects"
    objects_root.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []

    for bucket in _buckets():
        try:
            keys = _list_keys(s3, bucket)
        except TierUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — unreachable endpoint / missing bucket → skip tier
            raise TierUnavailable(f"cannot list bucket {bucket}: {exc}") from exc

        bucket_dir = objects_root / bucket
        bucket_dir.mkdir(parents=True, exist_ok=True)
        index: dict[str, dict[str, Any]] = {}
        total = 0
        for k in keys:
            key = k["Key"]
            local = bucket_dir / key
            local.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(local))
            digest = sha256_file(local)
            index[key] = {"size": local.stat().st_size, "etag": k["ETag"], "sha256": digest}
            total += local.stat().st_size
        index_text = json.dumps(index, indent=2, sort_keys=True)
        (bucket_dir / "_index.json").write_text(index_text)
        items.append(
            {
                "bucket": bucket,
                "dir": f"objects/{bucket}",
                "object_count": len(index),
                "total_bytes": total,
                "sha256": sha256_text(index_text),  # tree-hash over the sorted index
                "status": OK,
            }
        )
    return TierResult("objects", status=rollup_status([i["status"] for i in items]), items=items)


def restore_objects_tier(bundle_dir: Path, *, force: bool = False) -> list[dict[str, Any]]:
    """Upload mirrored objects back to their buckets. Refuses non-empty buckets without ``force``."""
    manifest = json.loads((bundle_dir / "bundle.manifest.json").read_text())
    s3 = _s3_client()
    out: list[dict[str, Any]] = []
    for item in manifest.get("tiers", {}).get("objects", {}).get("items", []):
        if item.get("status") != OK:
            continue
        bucket = item["bucket"]
        bucket_dir = bundle_dir / item["dir"]
        index = json.loads((bucket_dir / "_index.json").read_text())
        if not force and _list_keys(s3, bucket):
            raise ValueError(f"bucket {bucket} is non-empty — pass force=True to overwrite")
        uploaded = 0
        for key in index:
            s3.upload_file(str(bucket_dir / key), bucket, key)
            uploaded += 1
        out.append({"bucket": bucket, "uploaded": uploaded})
    return out
