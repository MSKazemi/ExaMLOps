"""Storage layer talks to MinIO via aioboto3. Tests use moto's mock_aws.

Two of these tests used to assert nothing at all: they called `ensure_bucket()` and `delete()`
and passed as long as neither raised. Proved by neutering each method to `return` — both stayed
green, so each named a behaviour it could not observe. They now look at the bucket afterwards.

The check goes through `storage._client()` rather than a plain sync boto3 client on purpose: the
package conftest adapts moto's stubber output to aiobotocore's async shape for the whole session,
so a sync client here would not talk to the same mock.
"""

import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from storage import ImageStorage


async def _bucket_exists(storage) -> bool:
    async with storage._client() as s3:
        try:
            await s3.head_bucket(Bucket=storage._bucket)
            return True
        except ClientError:
            return False


async def _object_exists(storage, key: str) -> bool:
    async with storage._client() as s3:
        try:
            await s3.head_object(Bucket=storage._bucket, Key=key)
            return True
        except ClientError:
            return False


@pytest.fixture
def storage():
    return ImageStorage(
        endpoint_url=None,  # no custom endpoint so moto can intercept
        access_key="test",
        secret_key="test",
        bucket="dashboard-model-docs",
        region="us-east-1",
    )


async def test_ensure_bucket_creates_when_missing(storage):
    with mock_aws():
        assert not await _bucket_exists(storage), "precondition: the bucket must start missing"
        await storage.ensure_bucket()
        assert await _bucket_exists(storage)
        await storage.ensure_bucket()  # idempotent
        assert await _bucket_exists(storage)


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
        assert await _object_exists(storage, "JPCP/x.png"), "precondition: the object must be there"
        await storage.delete("JPCP/x.png")
        assert not await _object_exists(storage, "JPCP/x.png")
        # ...and only now is the second call genuinely a delete of a missing object.
        await storage.delete("JPCP/x.png")
        assert not await _object_exists(storage, "JPCP/x.png")
