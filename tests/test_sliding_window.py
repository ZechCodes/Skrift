"""Tests for sliding window counter backends.

Covers the shared async protocol: in-memory and Redis backends must behave
identically for the rate-limit middleware and failed-auth tracker.
"""

from __future__ import annotations

import asyncio

import pytest

from skrift.lib.sliding_window import InMemorySlidingWindowCounter

try:
    import fakeredis.aioredis as fake_aioredis
    from skrift.lib.sliding_window_redis import RedisSlidingWindowCounter

    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


async def _make_redis_counter(window: float = 60.0, prefix: str = "test:ratelimit"):
    client = fake_aioredis.FakeRedis()
    return RedisSlidingWindowCounter(client, window=window, prefix=prefix), client


@pytest.fixture
def in_memory():
    return InMemorySlidingWindowCounter(window=60.0)


# ---------------------------------------------------------------------------
# Shared behavior: parameterize across backends.
# ---------------------------------------------------------------------------


async def _build_backend(kind: str):
    if kind == "memory":
        return InMemorySlidingWindowCounter(window=60.0), None
    counter, client = await _make_redis_counter()
    return counter, client


BACKENDS = ["memory"] + (["redis"] if HAS_FAKEREDIS else [])


@pytest.mark.parametrize("backend", BACKENDS)
class TestSharedBehavior:
    @pytest.mark.asyncio
    async def test_allows_under_limit(self, backend):
        counter, client = await _build_backend(backend)
        try:
            allowed, retry_after = await counter.check_and_record("alice", limit=3)
            assert allowed is True
            assert retry_after == 0
        finally:
            if client is not None:
                await client.aclose()

    @pytest.mark.asyncio
    async def test_blocks_at_limit(self, backend):
        counter, client = await _build_backend(backend)
        try:
            for _ in range(3):
                allowed, _ = await counter.check_and_record("bob", limit=3)
                assert allowed is True
            allowed, retry_after = await counter.check_and_record("bob", limit=3)
            assert allowed is False
            assert retry_after >= 1
        finally:
            if client is not None:
                await client.aclose()

    @pytest.mark.asyncio
    async def test_keys_are_isolated(self, backend):
        counter, client = await _build_backend(backend)
        try:
            for _ in range(2):
                allowed, _ = await counter.check_and_record("alice", limit=2)
                assert allowed is True
            # Alice is at limit but Bob has his own bucket
            allowed, _ = await counter.check_and_record("alice", limit=2)
            assert allowed is False
            allowed, _ = await counter.check_and_record("bob", limit=2)
            assert allowed is True
        finally:
            if client is not None:
                await client.aclose()

    @pytest.mark.asyncio
    async def test_record_and_count(self, backend):
        counter, client = await _build_backend(backend)
        try:
            assert await counter.count("alice") == 0
            await counter.record("alice")
            await counter.record("alice")
            assert await counter.count("alice") == 2
        finally:
            if client is not None:
                await client.aclose()


# ---------------------------------------------------------------------------
# Redis-specific: shared state across counter instances (the whole point).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")
class TestRedisBackedSharedState:
    @pytest.mark.asyncio
    async def test_two_counters_share_limit(self):
        """The point of Redis: two app replicas backed by the same Redis
        enforce a single combined limit."""
        client = fake_aioredis.FakeRedis()
        c1 = RedisSlidingWindowCounter(client, window=60.0, prefix="test")
        c2 = RedisSlidingWindowCounter(client, window=60.0, prefix="test")
        try:
            # Replica 1 records 2 hits, replica 2 records 1 — total 3.
            for _ in range(2):
                allowed, _ = await c1.check_and_record("shared", limit=3)
                assert allowed is True
            allowed, _ = await c2.check_and_record("shared", limit=3)
            assert allowed is True

            # 4th attempt from either replica should be rejected
            allowed, retry = await c1.check_and_record("shared", limit=3)
            assert allowed is False
            assert retry >= 1

            allowed, _ = await c2.check_and_record("shared", limit=3)
            assert allowed is False
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_atomic_race(self):
        """Concurrent calls don't exceed the limit.

        Without the Lua script, two concurrent calls could both see count=limit-1,
        both insert, and end up at limit+1.
        """
        client = fake_aioredis.FakeRedis()
        counter = RedisSlidingWindowCounter(client, window=60.0, prefix="test")
        try:
            # Fire 20 concurrent check_and_record calls against a limit of 5.
            results = await asyncio.gather(*[
                counter.check_and_record("contested", limit=5) for _ in range(20)
            ])
            allowed_count = sum(1 for allowed, _ in results if allowed)
            assert allowed_count == 5
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_prefix_isolates_keys(self):
        client = fake_aioredis.FakeRedis()
        c_a = RedisSlidingWindowCounter(client, window=60.0, prefix="app-a")
        c_b = RedisSlidingWindowCounter(client, window=60.0, prefix="app-b")
        try:
            for _ in range(2):
                allowed, _ = await c_a.check_and_record("same-key", limit=2)
                assert allowed is True
            # app-b's bucket is untouched
            allowed, _ = await c_b.check_and_record("same-key", limit=2)
            assert allowed is True
        finally:
            await client.aclose()
