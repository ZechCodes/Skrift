"""Tests for the backend-aware rate limiter primitive (skrift.ratelimit)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skrift.config import RedisConfig
from skrift.ratelimit import (
    RateLimiter,
    Verdict,
    get_limiter,
    normalize_window,
    reset_limiter,
)

try:
    import fakeredis.aioredis as fake_aioredis

    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


class TestNormalizeWindow:
    def test_named_periods(self):
        assert normalize_window("second") == 1.0
        assert normalize_window("minute") == 60.0
        assert normalize_window("hour") == 3600.0
        assert normalize_window("day") == 86400.0

    def test_numeric_passthrough(self):
        assert normalize_window(30) == 30.0
        assert normalize_window(90.5) == 90.5

    def test_unknown_period_raises(self):
        with pytest.raises(ValueError):
            normalize_window("fortnight")


class TestInMemoryLimiter:
    @pytest.mark.asyncio
    async def test_single_window_allows_then_blocks(self):
        limiter = RateLimiter(redis_client=None)
        v1 = await limiter.check("capture", "1.2.3.4", [(1, "minute")])
        assert isinstance(v1, Verdict)
        assert v1.allowed is True
        assert v1.retry_after == 0

        v2 = await limiter.check("capture", "1.2.3.4", [(1, "minute")])
        assert v2.allowed is False
        assert 1 <= v2.retry_after <= 60

    @pytest.mark.asyncio
    async def test_multi_window_and_logic(self):
        # 1/min + 6/hr per the motivating example.
        limiter = RateLimiter(redis_client=None)
        limits = [(1, "minute"), (6, "hour")]
        assert (await limiter.check("inquiry", "ip", limits)).allowed is True
        assert (await limiter.check("inquiry", "ip", limits)).allowed is False

    @pytest.mark.asyncio
    async def test_names_are_isolated(self):
        limiter = RateLimiter(redis_client=None)
        await limiter.check("a", "ip", [(1, "minute")])
        # Same key, different name → independent bucket.
        assert (await limiter.check("b", "ip", [(1, "minute")])).allowed is True

    @pytest.mark.asyncio
    async def test_keys_are_isolated(self):
        limiter = RateLimiter(redis_client=None)
        await limiter.check("a", "ip1", [(1, "minute")])
        assert (await limiter.check("a", "ip2", [(1, "minute")])).allowed is True

    def test_backend_label(self):
        assert RateLimiter(redis_client=None).backend == "memory"

    @pytest.mark.asyncio
    async def test_get_counter_reuses_instance(self):
        limiter = RateLimiter(redis_client=None)
        c1 = limiter.get_counter(60.0, "failed_auth")
        c2 = limiter.get_counter(60.0, "failed_auth")
        assert c1 is c2
        # record/count surface works for the failed-auth style consumer
        await c1.record("ip")
        assert await c1.count("ip") == 1


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")
class TestRedisLimiter:
    @pytest.mark.asyncio
    async def test_backend_label_and_check(self):
        client = fake_aioredis.FakeRedis()
        limiter = RateLimiter(redis_client=client, redis_config=RedisConfig(prefix="myapp"))
        try:
            assert limiter.backend == "redis"
            limits = [(1, "minute"), (6, "hour")]
            assert (await limiter.check("inquiry", "ip", limits)).allowed is True
            assert (await limiter.check("inquiry", "ip", limits)).allowed is False
        finally:
            await limiter.aclose()

    @pytest.mark.asyncio
    async def test_key_namespacing_includes_prefix_and_name(self):
        client = fake_aioredis.FakeRedis()
        limiter = RateLimiter(redis_client=client, redis_config=RedisConfig(prefix="myapp"))
        try:
            await limiter.check("inquiry", "1.2.3.4", [(5, "minute")])
            keys = [k.decode() if isinstance(k, bytes) else k for k in await client.keys("*")]
            assert any(
                k.startswith("myapp:skrift:ratelimit:inquiry:") and "1.2.3.4" in k
                for k in keys
            ), keys
        finally:
            await limiter.aclose()

    @pytest.mark.asyncio
    async def test_two_limiters_share_redis_state(self):
        client = fake_aioredis.FakeRedis()
        a = RateLimiter(redis_client=client, redis_config=RedisConfig())
        b = RateLimiter(redis_client=client, redis_config=RedisConfig())
        try:
            assert (await a.check("inquiry", "ip", [(1, "minute")])).allowed is True
            # Second replica sees the first replica's hit.
            assert (await b.check("inquiry", "ip", [(1, "minute")])).allowed is False
        finally:
            await client.aclose()


class TestFromSettings:
    def test_no_redis_url_uses_memory(self):
        settings = MagicMock()
        settings.redis.url = ""
        settings.redis = RedisConfig(url="")
        limiter = RateLimiter.from_settings(settings)
        assert limiter.backend == "memory"


class TestGetLimiterSingleton:
    def teardown_method(self):
        reset_limiter()

    def test_returns_same_instance(self, monkeypatch):
        from skrift import ratelimit

        settings = MagicMock()
        settings.redis = RedisConfig(url="")
        monkeypatch.setattr(ratelimit, "get_settings", lambda: settings)
        reset_limiter()

        first = get_limiter()
        second = get_limiter()
        assert first is second

    def test_reset_rebuilds(self, monkeypatch):
        from skrift import ratelimit

        settings = MagicMock()
        settings.redis = RedisConfig(url="")
        monkeypatch.setattr(ratelimit, "get_settings", lambda: settings)
        reset_limiter()

        first = get_limiter()
        reset_limiter()
        assert get_limiter() is not first
