"""Tests for the Redis-backed bot state store.

Uses a small fake Redis client that implements just the methods we
call (``get``, ``set`` with ``ex=``, ``delete``). Avoids a live Redis
dependency in unit tests but verifies the wiring: prefixing,
namespacing, byte/string round-trip, TTL passthrough.
"""

from __future__ import annotations

from typing import Any

import pytest

from skrift.bot_detection.store import RedisBotStateStore


class FakeRedis:
    """Minimal in-memory stand-in for redis.asyncio.Redis."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple[str, ...]] = []

    async def get(self, key: str) -> Any:
        self.calls.append(("get", key))
        return self.store.get(key)

    async def set(
        self, key: str, value: str, ex: int | None = None
    ) -> None:
        self.calls.append(("set", key, value, str(ex)))
        self.store[key] = value.encode() if isinstance(value, str) else value
        if ex is not None:
            self.ttls[key] = ex

    async def delete(self, key: str) -> None:
        self.calls.append(("delete", key))
        self.store.pop(key, None)
        self.ttls.pop(key, None)


class TestRedisBotStateStore:
    @pytest.mark.asyncio
    async def test_set_uses_full_key_with_prefix(self):
        client = FakeRedis()
        store = RedisBotStateStore(client, prefix="myapp:bd")
        await store.set("trap_hit", "1.2.3.4", "/path", ttl=600)
        assert ("set", "myapp:bd:trap_hit:1.2.3.4", "/path", "600") in client.calls

    @pytest.mark.asyncio
    async def test_get_returns_string_value(self):
        client = FakeRedis()
        store = RedisBotStateStore(client, prefix="bd")
        await store.set("ns", "key", "value", ttl=60)
        assert await store.get("ns", "key") == "value"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self):
        client = FakeRedis()
        store = RedisBotStateStore(client, prefix="bd")
        assert await store.get("ns", "missing") is None

    @pytest.mark.asyncio
    async def test_get_decodes_bytes(self):
        client = FakeRedis()
        store = RedisBotStateStore(client, prefix="bd")
        client.store["bd:ns:key"] = b"hello"
        assert await store.get("ns", "key") == "hello"

    @pytest.mark.asyncio
    async def test_delete_removes_key(self):
        client = FakeRedis()
        store = RedisBotStateStore(client, prefix="bd")
        await store.set("ns", "key", "v", ttl=60)
        await store.delete("ns", "key")
        assert await store.get("ns", "key") is None

    @pytest.mark.asyncio
    async def test_namespace_isolation(self):
        client = FakeRedis()
        store = RedisBotStateStore(client, prefix="bd")
        await store.set("trap_hit", "1.2.3.4", "/trap", ttl=60)
        await store.set("pixel_loaded", "1.2.3.4", "pixel", ttl=60)
        assert await store.get("trap_hit", "1.2.3.4") == "/trap"
        assert await store.get("pixel_loaded", "1.2.3.4") == "pixel"

    @pytest.mark.asyncio
    async def test_factory_picks_redis_when_client_provided(self):
        from skrift.bot_detection.config import BotDetectionConfig
        from skrift.bot_detection.factory import build_bot_state_store
        from skrift.config import RedisConfig

        client = FakeRedis()
        store = build_bot_state_store(
            BotDetectionConfig(cache_backend="redis"),
            RedisConfig(prefix="myapp"),
            redis_client=client,
        )
        assert isinstance(store, RedisBotStateStore)
        await store.set("ns", "key", "v", ttl=10)
        assert ("set", "myapp:skrift:bot_detection:ns:key", "v", "10") in client.calls

    @pytest.mark.asyncio
    async def test_factory_falls_back_to_memory_when_redis_missing(self):
        from skrift.bot_detection.config import BotDetectionConfig
        from skrift.bot_detection.factory import build_bot_state_store
        from skrift.bot_detection.store import InMemoryBotStateStore
        from skrift.config import RedisConfig

        store = build_bot_state_store(
            BotDetectionConfig(cache_backend="redis"),
            RedisConfig(),
            redis_client=None,
        )
        assert isinstance(store, InMemoryBotStateStore)

    @pytest.mark.asyncio
    async def test_factory_picks_memory_when_configured(self):
        from skrift.bot_detection.config import BotDetectionConfig
        from skrift.bot_detection.factory import build_bot_state_store
        from skrift.bot_detection.store import InMemoryBotStateStore
        from skrift.config import RedisConfig

        client = FakeRedis()
        store = build_bot_state_store(
            BotDetectionConfig(cache_backend="memory"),
            RedisConfig(),
            redis_client=client,  # provided but ignored
        )
        assert isinstance(store, InMemoryBotStateStore)
