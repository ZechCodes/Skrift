"""Storage backend protocol and common types."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class StoredFile:
    """Metadata for a file stored in a backend."""

    key: str
    url: str
    content_type: str
    size: int
    content_hash: str


@runtime_checkable
class StorageBackend(Protocol):
    """Interface for pluggable asset storage backends."""

    async def put(self, key: str, data: bytes, content_type: str) -> StoredFile:
        """Store data under the given key."""
        ...

    async def get(self, key: str) -> bytes:
        """Retrieve the raw bytes for a key."""
        ...

    async def delete(self, key: str) -> None:
        """Remove a key from storage."""
        ...

    async def exists(self, key: str) -> bool:
        """Check whether a key exists in storage."""
        ...

    async def list_keys(self, prefix: str = "") -> AsyncIterator[str]:
        """Yield all keys matching the given prefix."""
        ...

    async def get_url(self, key: str) -> str:
        """Return a public or signed URL for the key."""
        ...
