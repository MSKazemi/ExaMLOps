"""Storage layer talks to MinIO via aioboto3. Tests use moto's mock_aws."""
import pytest
from moto import mock_aws
from settings import settings
from storage import ImageStorage


@pytest.fixture
def storage():
    return ImageStorage(
        endpoint_url=settings.minio_url,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.dashboard_minio_bucket,
        region="us-east-1",
    )


async def test_ensure_bucket_creates_when_missing(storage):
    with mock_aws():
        await storage.ensure_bucket()
        await storage.ensure_bucket()  # idempotent


async def test_put_and_presign(storage):
    with mock_aws():
        await storage.ensure_bucket()
        await storage.put(
            key="JPCP/test.png",
            data=b"\x89PNG\r\n\x1a\nfake",
            content_type="image/png",
        )
        url = await storage.presigned_get_url("JPCP/test.png", expires=60)
        assert "JPCP/test.png" in url
        assert "X-Amz-Signature" in url or "Signature" in url


async def test_delete(storage):
    with mock_aws():
        await storage.ensure_bucket()
        await storage.put(key="JPCP/x.png", data=b"x", content_type="image/png")
        await storage.delete("JPCP/x.png")
        await storage.delete("JPCP/x.png")  # missing object must not raise
