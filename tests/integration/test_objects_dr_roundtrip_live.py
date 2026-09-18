"""Restore the model artifacts against a real object store — step one of a full disaster recovery.

`docs/guides/backup-restore.md` puts **objects first** in its recovery order: MinIO artifacts have
to exist before the model registry's references to them resolve. Everything covering that step ran
against an in-memory double, and the double was more forgiving than the real thing in the one way
that mattered — its `upload_file` created the bucket on demand, so restoring into a store that had
lost its buckets passed in the suite and failed against MinIO with `NoSuchBucket`. That is the
state a real disaster leaves, and it was the documented first step.

This drill runs the round trip against a real MinIO: put objects, back them up, **destroy the
bucket**, restore, and compare bytes — not counts. Counting objects would pass over a restore that
put back the right number of empty files.

Opt in with `EXAMLOPS_CHAOS_LIVE=1`; it starts and removes its own MinIO. `make chaos-drills` runs
it with the rest of the set.

    EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_objects_dr_roundtrip_live.py -s
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_CHAOS_LIVE") != "1", reason="set EXAMLOPS_CHAOS_LIVE=1"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker to start a MinIO"),
]

IMAGE = "minio/minio:latest"
USER = PASSWORD = "minioadmin"
BUCKET = "mlflow-artifacts"

#: What a model registry actually holds: a binary, something nested, and an empty marker.
CONTENT = {
    "models/jpcp/17/model.pkl": b"\x80\x05\x95 not really a pickle \x00\x01\x02",
    "models/jpcp/17/MLmodel": b"flavors:\n  python_function:\n    loader_module: mlflow.sklearn\n",
    "models/jpcp/17/conda.yaml": b"name: mlflow-env\n",
    "experiments/3/abc123/artifacts/plot.png": bytes(range(256)) * 8,
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def endpoint() -> str:
    name = f"exa-dr-minio-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", f"MINIO_ROOT_USER={USER}", "-e", f"MINIO_ROOT_PASSWORD={PASSWORD}",
            "-p", f"127.0.0.1:{port}:9000", IMAGE, "server", "/data",
        ],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", name, "curl", "-sf", "http://127.0.0.1:9000/minio/health/live"],
                capture_output=True,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail("MinIO never became ready")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def s3(endpoint, monkeypatch):
    """A client on a private bucket, with the tier pointed at the same store."""
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", endpoint)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", USER)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", PASSWORD)
    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", BUCKET)
    client = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=USER, aws_secret_access_key=PASSWORD
    )
    for existing in client.list_buckets().get("Buckets", []):
        _empty_and_delete(client, existing["Name"])
    client.create_bucket(Bucket=BUCKET)
    return client


def _empty_and_delete(client, bucket: str) -> None:
    token = None
    while True:
        kw = {"Bucket": bucket}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    client.delete_bucket(Bucket=bucket)


def _digests(client) -> dict[str, str]:
    out: dict[str, str] = {}
    token = None
    while True:
        kw = {"Bucket": BUCKET}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            body = client.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
            out[obj["Key"]] = hashlib.sha256(body).hexdigest()
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return out


def _bundle(tmp_path: Path):
    from examlops.backup import objects_tier

    dest = tmp_path / "bundle"
    dest.mkdir(parents=True, exist_ok=True)
    res = objects_tier.backup_objects_tier(dest)
    (dest / "bundle.manifest.json").write_text(
        json.dumps({"tiers": {"objects": {"items": res.items}}})
    )
    return res, dest


def test_the_artifacts_come_back_after_the_bucket_is_destroyed(s3, tmp_path):
    """Put → back up → delete the bucket → restore → **every byte** is the one that went in."""
    from examlops.backup import objects_tier

    for key, body in CONTENT.items():
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)
    before = _digests(s3)
    assert len(before) == len(CONTENT)

    res, bundle_dir = _bundle(tmp_path)
    print("\nbackup:", res.status, [(i["bucket"], i.get("object_count")) for i in res.items])
    assert res.status == "ok"

    _empty_and_delete(s3, BUCKET)  # the object store lost the bucket itself
    assert BUCKET not in [b["Name"] for b in s3.list_buckets().get("Buckets", [])]

    out = objects_tier.restore_objects_tier(bundle_dir, force=True)
    print("restore:", out)
    assert out[0]["ok"] is True, out
    assert out[0]["bucket_created"] is True, "the bucket was gone; the restore had to recreate it"

    after = _digests(s3)
    assert after == before, (
        "the objects that came back are not the objects that went in — comparing counts instead "
        "of digests is how a restore of the right number of wrong files passes"
    )
    print(f"recovered {len(after)} objects, digests identical")


def test_listing_survives_more_objects_than_one_page(s3, tmp_path):
    """A page of `list_objects_v2` is 1000 keys, and the in-memory double never truncates.

    A registry with a few hundred model versions passes that line quickly, and a backup that
    silently stops at the first page is the worst kind: it succeeds, it verifies, and it is missing
    everything after key 1000.
    """
    from examlops.backup import objects_tier

    keys = [f"bulk/{i:05d}.txt" for i in range(1050)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda k: s3.put_object(Bucket=BUCKET, Key=k, Body=k.encode()), keys))

    listed = objects_tier._list_keys(s3, BUCKET)
    assert len(listed) == len(keys), (
        f"listed {len(listed)} of {len(keys)} objects — pagination stops short, so everything "
        "after the first page is missing from every backup this tier takes"
    )

    res, _ = _bundle(tmp_path)
    print("\nbulk backup:", res.items[0]["object_count"], "objects")
    assert res.items[0]["object_count"] == len(keys)
