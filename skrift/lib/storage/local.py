"""Local filesystem storage backend."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

from skrift.lib.storage.base import StoredFile


class LocalStorageBackend:
    """Store files on the local filesystem with content-hash subdirectories."""

    def __init__(self, base_path: Path, store_name: str = "default") -> None:
        self._base_path = base_path
        self._store_name = store_name

    async def put(self, key: str, data: bytes, content_type: str) -> StoredFile:
        path = self._key_to_path(key)
        await asyncio.to_thread(self._write_file, path, data)
        return StoredFile(
            key=key,
            url=self._build_url(key),
            content_type=content_type,
            size=len(data),
            content_hash=hashlib.sha256(data).hexdigest(),
        )

    async def get(self, key: str) -> bytes:
        path = self._key_to_path(key)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._key_to_path(key)
        await asyncio.to_thread(self._unlink, path)

    async def exists(self, key: str) -> bool:
        path = self._key_to_path(key)
        return await asyncio.to_thread(path.exists)

    async def list_keys(self, prefix: str = "") -> AsyncIterator[str]:
        base = self._base_path
        for path in await asyncio.to_thread(self._walk, base):
            key = str(path.relative_to(base))
            if key.startswith(prefix):
                yield key

    async def get_url(self, key: str) -> str:
        return self._build_url(key)

    # -- internal helpers --

    def _key_to_path(self, key: str) -> Path:
        """Map a key to a content-hash based subdirectory path."""
        # Keys are expected to be content hashes (or hash-based); use first 4
        # chars for two levels of directory fan-out.
        if len(key) >= 4:
            return self._base_path / key[:2] / key[2:4] / key
        return self._base_path / key

    def _build_url(self, key: str) -> str:
        return f"/storage/{self._store_name}/{key}"

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _unlink(path: Path) -> None:
        path.unlink(missing_ok=True)

    @staticmethod
    def _walk(base: Path) -> list[Path]:
        if not base.exists():
            return []
        return [p for p in base.rglob("*") if p.is_file()]
