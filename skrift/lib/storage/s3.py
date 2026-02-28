"""S3-compatible storage backend (requires ``pip install skrift[s3]``)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

try:
    import aioboto3
except ImportError as exc:
    raise ImportError(
        "S3 storage backend requires aioboto3. Install it with: pip install skrift[s3]"
    ) from exc

from skrift.lib.storage.base import StoredFile

if TYPE_CHECKING:
    from skrift.config import S3Config


class S3StorageBackend:
    """Store files in an S3-compatible bucket."""

    def __init__(self, config: S3Config) -> None:
        self._config = config
        self._session = aioboto3.Session()

    def _client_kwargs(self) -> dict:
        kwargs: dict = {
            "region_name": self._config.region,
        }
        if self._config.endpoint_url:
            kwargs["endpoint_url"] = self._config.endpoint_url
        if self._config.access_key_id:
            kwargs["aws_access_key_id"] = self._config.access_key_id
        if self._config.secret_access_key:
            kwargs["aws_secret_access_key"] = self._config.secret_access_key
        return kwargs

    def _full_key(self, key: str) -> str:
        if self._config.prefix:
            return f"{self._config.prefix.rstrip('/')}/{key}"
        return key

    async def put(self, key: str, data: bytes, content_type: str) -> StoredFile:
        full_key = self._full_key(key)
        put_kwargs: dict = {
            "Bucket": self._config.bucket,
            "Key": full_key,
            "Body": data,
            "ContentType": content_type,
        }
        if self._config.acl:
            put_kwargs["ACL"] = self._config.acl

        async with self._session.client("s3", **self._client_kwargs()) as s3:
            await s3.put_object(**put_kwargs)

        return StoredFile(
            key=key,
            url=await self.get_url(key),
            content_type=content_type,
            size=len(data),
            content_hash=hashlib.sha256(data).hexdigest(),
        )

    async def get(self, key: str) -> bytes:
        full_key = self._full_key(key)
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            response = await s3.get_object(Bucket=self._config.bucket, Key=full_key)
            return await response["Body"].read()

    async def delete(self, key: str) -> None:
        full_key = self._full_key(key)
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            await s3.delete_object(Bucket=self._config.bucket, Key=full_key)

    async def exists(self, key: str) -> bool:
        full_key = self._full_key(key)
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            try:
                await s3.head_object(Bucket=self._config.bucket, Key=full_key)
                return True
            except s3.exceptions.ClientError:
                return False

    async def list_keys(self, prefix: str = "") -> AsyncIterator[str]:
        full_prefix = self._full_key(prefix)
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=self._config.bucket, Prefix=full_prefix
            ):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Strip prefix to return relative keys
                    if self._config.prefix and key.startswith(self._config.prefix):
                        key = key[len(self._config.prefix.rstrip("/")) + 1:]
                    yield key

    async def get_url(self, key: str) -> str:
        full_key = self._full_key(key)

        # CDN / custom public URL
        if self._config.public_url:
            base = self._config.public_url.rstrip("/")
            return f"{base}/{full_key}"

        # Public-read bucket: standard S3 URL
        if self._config.acl == "public-read":
            if self._config.endpoint_url:
                base = self._config.endpoint_url.rstrip("/")
                return f"{base}/{self._config.bucket}/{full_key}"
            return f"https://{self._config.bucket}.s3.{self._config.region}.amazonaws.com/{full_key}"

        # Private: generate presigned URL
        async with self._session.client("s3", **self._client_kwargs()) as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._config.bucket, "Key": full_key},
                ExpiresIn=self._config.presign_ttl,
            )

    async def close(self) -> None:
        """No persistent resources to clean up."""
