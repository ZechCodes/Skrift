"""Cross-request state store for bot detection metrics.

Deferred metrics — pixel beacon, JS challenge, robots honeypot — record
state on one request and read it on a later request from the same
client. The store abstraction lets the same metric work against either
a process-local dict (for single-process deployments and tests) or
Redis (for multi-process / multi-replica deployments).

The store carries opaque string values keyed by ``(namespace, key)``
tuples. Time-to-live is enforced lazily for the in-memory backend, which
also sweeps expired entries and caps its size so abandoned client state
can't accumulate.
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


DEFAULT_SWEEP_EVERY = 256
DEFAULT_MAX_ENTRIES = 50_000


class InMemoryBotStateStore:
    """Process-local store backed by a dict. Not safe across replicas.

    TTLs are enforced lazily on read; expired entries are dropped on
    access. Keys are client identities (usually IPs), and a client that
    trips one metric and never comes back is never read again — so
    reads alone can't keep the dict bounded. Two guards do:

    * every ``sweep_every`` writes, expired entries are dropped in one
      amortized pass;
    * past ``max_entries`` live entries, the oldest writes are evicted.
      Evicting live state only means that client is measured afresh.
    """

    def __init__(
        self,
        *,
        sweep_every: int = DEFAULT_SWEEP_EVERY,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._data: dict[tuple[str, str], tuple[str, float]] = {}
        self._sweep_every = sweep_every
        self._max_entries = max_entries
        self._writes_since_sweep = 0

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
        # Re-inserting rather than overwriting keeps dict order by write
        # time, which is the order eviction walks.
        self._data.pop((namespace, key), None)
        self._data[(namespace, key)] = (value, time.monotonic() + ttl)

        self._writes_since_sweep += 1
        if self._writes_since_sweep >= self._sweep_every:
            self._sweep_expired()
        self._evict_oldest_over_capacity()

    async def delete(self, namespace: str, key: str) -> None:
        self._data.pop((namespace, key), None)

    def _sweep_expired(self) -> None:
        now = time.monotonic()
        self._writes_since_sweep = 0
        self._data = {
            entry_key: entry
            for entry_key, entry in self._data.items()
            if entry[1] > now
        }

    def _evict_oldest_over_capacity(self) -> None:
        while len(self._data) > self._max_entries:
            self._data.pop(next(iter(self._data)))


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
