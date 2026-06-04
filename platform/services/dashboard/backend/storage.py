"""MinIO async storage helper for dashboard image uploads."""
from __future__ import annotations

import aioboto3
from botocore.exceptions import ClientError


class ImageStorage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
    ) -> None:
        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._region = region
        self._session = aioboto3.Session()

    def _client(self):
        kwargs: dict = {
            "aws_access_key_id": self._access_key,
            "aws_secret_access_key": self._secret_key,
            "region_name": self._region,
        }
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        return self._session.client("s3", **kwargs)

    async def ensure_bucket(self) -> None:
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self._bucket)
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("403", "404", "NoSuchBucket"):
                    await s3.create_bucket(Bucket=self._bucket)
                    return
                raise

    async def put(self, *, key: str, data: bytes, content_type: str) -> None:
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            try:
                await s3.delete_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in ("404", "NoSuchKey"):
                    raise

    async def presigned_get_url(self, key: str, *, expires: int) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires,
            )
