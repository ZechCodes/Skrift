"""Cross-request state store for bot detection metrics.

Deferred metrics — pixel beacon, JS challenge, robots honeypot — record
state on one request and read it on a later request from the same
client. The store abstraction lets the same metric work against either
a process-local dict (for single-process deployments and tests) or
Redis (for multi-process / multi-replica deployments).

The store carries opaque string values keyed by ``(namespace, key)``
tuples. Time-to-live is enforced lazily for the in-memory backend.
"""

from __future__ import annotations

import time
from typing import Protocol


class BotStateStore(Protocol):
    """Minimal key/value with TTL used by deferred metrics."""

    async def get(self, namespace: str, key: str) -> str | None: ...

    async def set(
        self, namespace: str, key: str, value: str, *, ttl: int
    ) -> None: ...

    async def delete(self, namespace: str, key: str) -> None: ...


class InMemoryBotStateStore:
    """Process-local store backed by a dict. Not safe across replicas.

    TTLs are enforced lazily on read; expired entries are dropped on
    access.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[str, float]] = {}

    async def get(self, namespace: str, key: str) -> str | None:
        entry = self._data.get((namespace, key))
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            self._data.pop((namespace, key), None)
            return None
        return value

    async def set(
        self, namespace: str, key: str, value: str, *, ttl: int
    ) -> None:
        self._data[(namespace, key)] = (value, time.monotonic() + ttl)

    async def delete(self, namespace: str, key: str) -> None:
        self._data.pop((namespace, key), None)


class RedisBotStateStore:
    """Redis-backed store. Used when ``settings.redis.url`` is set."""

    def __init__(self, client, prefix: str) -> None:
        self._client = client
        self._prefix = prefix

    def _full_key(self, namespace: str, key: str) -> str:
        return f"{self._prefix}:{namespace}:{key}"

    async def get(self, namespace: str, key: str) -> str | None:
        raw = await self._client.get(self._full_key(namespace, key))
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)

    async def set(
        self, namespace: str, key: str, value: str, *, ttl: int
    ) -> None:
        await self._client.set(self._full_key(namespace, key), value, ex=ttl)

    async def delete(self, namespace: str, key: str) -> None:
        await self._client.delete(self._full_key(namespace, key))
