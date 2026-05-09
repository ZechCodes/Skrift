"""Blob storage for oversized agent audit fields."""

from __future__ import annotations

import hashlib
import json
from base64 import b64decode, b64encode
from typing import Any

from skrift.agents.config import build_blob_store, get_agents_config
from skrift.agents.models import BlobRef

DEFAULT_LARGE_VALUE_THRESHOLD_BYTES = 262_144
BLOB_STREAM_PREFIX = "agents:blobs:"


class InMemoryBlobStore:
    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}

    async def put(
        self,
        value: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> BlobRef:
        digest = hashlib.sha256(value).hexdigest()
        blob_id = f"sha256:{digest}"
        self._values[blob_id] = value
        return BlobRef(
            blob_id=blob_id,
            hash=f"sha256:{digest}",
            size=len(value),
            content_type=content_type,
        )

    async def get(self, blob_ref: BlobRef) -> bytes:
        value = self._values[blob_ref.blob_id]
        digest = hashlib.sha256(value).hexdigest()
        if blob_ref.hash != f"sha256:{digest}":
            raise BlobIntegrityError(f"Blob hash mismatch for {blob_ref.blob_id}")
        return value

    async def exists(self, blob_ref: BlobRef) -> bool:
        return blob_ref.blob_id in self._values

    async def delete(self, blob_ref: BlobRef) -> None:
        self._values.pop(blob_ref.blob_id, None)


class ArchiveBlobStore:
    """Content-addressed blob store backed by the worker Archive."""

    def __init__(self, archive: Any | None = None) -> None:
        self._archive = archive

    async def put(
        self,
        value: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> BlobRef:
        digest = hashlib.sha256(value).hexdigest()
        blob_id = f"sha256:{digest}"
        blob_ref = BlobRef(
            blob_id=blob_id,
            hash=f"sha256:{digest}",
            size=len(value),
            content_type=content_type,
        )
        stream = self._stream(blob_id)
        if not await self._archive_has_blob(stream):
            await self.archive.bulk_insert_events(
                [
                    (
                        stream,
                        0,
                        {
                            "type": "BlobStored",
                            "payload": {
                                "blob_id": blob_id,
                                "hash": blob_ref.hash,
                                "size": len(value),
                                "content_type": content_type,
                                "data": b64encode(value).decode("ascii"),
                            },
                        },
                    )
                ]
            )
        return blob_ref

    async def get(self, blob_ref: BlobRef) -> bytes:
        rows = await self.archive.query_events(self._stream(blob_ref.blob_id))
        if not rows:
            raise KeyError(blob_ref.blob_id)
        event = rows[-1][1]
        payload = event.get("payload", {})
        raw = b64decode(str(payload["data"]).encode("ascii"))
        digest = hashlib.sha256(raw).hexdigest()
        if blob_ref.hash != f"sha256:{digest}":
            raise BlobIntegrityError(f"Blob hash mismatch for {blob_ref.blob_id}")
        if blob_ref.size != len(raw):
            raise BlobIntegrityError(f"Blob size mismatch for {blob_ref.blob_id}")
        return raw

    async def exists(self, blob_ref: BlobRef) -> bool:
        return await self._archive_has_blob(self._stream(blob_ref.blob_id))

    async def delete(self, blob_ref: BlobRef) -> None:
        """Archive blobs are append-only; deletion is handled by archive retention."""

    @property
    def archive(self) -> Any:
        if self._archive is not None:
            return self._archive
        from skrift.workers import get_runtime

        return get_runtime().archive

    async def _archive_has_blob(self, stream: str) -> bool:
        return bool(await self.archive.query_events(stream, to_position=0))

    @staticmethod
    def _stream(blob_id: str) -> str:
        return f"{BLOB_STREAM_PREFIX}{blob_id}"


class BlobIntegrityError(ValueError):
    """Raised when a blob fails hash verification."""


_blob_store: InMemoryBlobStore | ArchiveBlobStore | None = None


def set_blob_store(blob_store: InMemoryBlobStore | ArchiveBlobStore | None) -> None:
    global _blob_store
    _blob_store = blob_store


def get_blob_store() -> InMemoryBlobStore | ArchiveBlobStore:
    global _blob_store
    if _blob_store is None:
        _blob_store = build_blob_store()
    return _blob_store


async def offload_large_payload_fields(
    event: dict[str, Any],
    *,
    threshold_bytes: int | None = None,
) -> dict[str, Any]:
    threshold = effective_large_value_threshold(threshold_bytes)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event
    updated_payload = dict(payload)
    changed = False
    for key, value in payload.items():
        if _is_blob_ref(value):
            continue
        encoded = json.dumps(value, sort_keys=True, default=str).encode()
        if len(encoded) <= threshold:
            continue
        blob_ref = await get_blob_store().put(encoded, content_type="application/json")
        updated_payload[key] = blob_ref.model_dump(mode="json", by_alias=True)
        changed = True
    if not changed:
        return event
    return {**event, "payload": updated_payload}


async def dereference_blob_refs(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event
    updated_payload = dict(payload)
    changed = False
    for key, value in payload.items():
        if not _is_blob_ref(value):
            continue
        blob_ref = BlobRef.model_validate(value)
        raw = await get_blob_store().get(blob_ref)
        updated_payload[key] = json.loads(raw.decode())
        changed = True
    if not changed:
        return event
    return {**event, "payload": updated_payload}


def _is_blob_ref(value: Any) -> bool:
    return isinstance(value, dict) and value.get("_offload") is True


def effective_large_value_threshold(configured: int | None = None) -> int:
    threshold = configured or get_agents_config().audit.large_value_threshold_bytes
    try:
        from skrift.workers import get_runtime

        backend_limit = getattr(get_runtime().event_log, "max_inline_size", None)
    except Exception:
        backend_limit = None
    if isinstance(backend_limit, int) and backend_limit > 0:
        return min(threshold, backend_limit)
    return threshold
