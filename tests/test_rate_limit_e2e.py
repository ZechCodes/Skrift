"""End-to-end rate limiting tests.

These exercise the *whole stack* the way ``skrift.asgi.create_app`` wires it —
a real ``Settings`` object → ``RateLimiter.from_settings(settings)`` →
``DefineMiddleware(RateLimitMiddleware, config=settings.rate_limit,
limiter=...)`` → real HTTP requests through ``TestClient``. The isolated unit
tests live in ``test_rate_limit.py`` / ``test_ratelimit.py`` /
``test_sliding_window.py``; these prove the construction path and the
distributed (Redis) path work together, not just in isolation.
"""

from __future__ import annotations

import pytest
from litestar import Litestar, get, post
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient

from skrift.auth.failed_auth import FailedAuthLimiter
from skrift.config import RateLimitConfig, Settings
from skrift.middleware.rate_limit import RateLimitMiddleware
from skrift.ratelimit import RateLimiter

try:
    import fakeredis.aioredis as fake_aioredis

    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


def _handlers():
    @post("/book/inquiry")
    async def inquiry() -> str:
        return "ok"

    @get("/public")
    async def public() -> str:
        return "ok"

    @get("/auth/login")
    async def auth_login() -> str:
        return "ok"

    return [inquiry, public, auth_login]


def _build_app(settings: Settings, limiter: RateLimiter | None = None) -> Litestar:
    """Wire rate limiting exactly as create_app does."""
    rate_limiter = limiter or RateLimiter.from_settings(settings)
    return Litestar(
        route_handlers=_handlers(),
        middleware=[
            DefineMiddleware(
                RateLimitMiddleware,
                config=settings.rate_limit,
                limiter=rate_limiter,
            )
        ],
    )


class TestDeclarativeRuleEndToEnd:
    """A declarative multi-window rule, carried through Settings and the
    from_settings() limiter, enforces a real 429 over HTTP."""

    def test_inquiry_minute_window_enforced_via_settings(self):
        settings = Settings(
            secret_key="test",
            rate_limit=RateLimitConfig(
                requests_per_minute=1000,
                rules=[
                    {
                        "match": {"path": "/book/inquiry", "method": "POST"},
                        "key": "ip",
                        "limits": [
                            {"limit": 1, "per": "minute"},
                            {"limit": 6, "per": "hour"},
                        ],
                    }
                ],
            ),
        )
        with TestClient(_build_app(settings)) as client:
            assert client.post("/book/inquiry").status_code == 201
            blocked = client.post("/book/inquiry")
            assert blocked.status_code == 429
            assert int(blocked.headers["retry-after"]) >= 1
            # The generous default still governs other routes.
            assert client.get("/public").status_code == 200

    def test_no_redis_url_uses_memory_backend(self):
        settings = Settings(secret_key="test", rate_limit=RateLimitConfig())
        limiter = RateLimiter.from_settings(settings)
        assert limiter.backend == "memory"


class TestLegacyConfigEndToEnd:
    """Legacy requests_per_minute / auth_requests_per_minute, parsed into
    Settings, still enforce limits over real HTTP (back-compat)."""

    def test_auth_path_stricter_than_default(self):
        settings = Settings(
            secret_key="test",
            rate_limit=RateLimitConfig(
                requests_per_minute=100,
                auth_requests_per_minute=2,
            ),
        )
        with TestClient(_build_app(settings)) as client:
            for _ in range(2):
                assert client.get("/auth/login").status_code == 200
            assert client.get("/auth/login").status_code == 429
            # Default bucket is independent and far from its limit.
            assert client.get("/public").status_code == 200


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")
class TestRedisBackedEndToEnd:
    """The distributed path: limiters backed by one Redis enforce a single
    combined limit across replicas, end-to-end over HTTP."""

    def test_two_replicas_share_one_limit(self):
        client_redis = fake_aioredis.FakeRedis()
        settings = Settings(
            secret_key="test",
            rate_limit=RateLimitConfig(
                rules=[
                    {
                        "match": {"path": "/book/inquiry", "method": "POST"},
                        "limits": [{"limit": 1, "per": "minute"}],
                    }
                ],
            ),
        )
        # Two app instances ("replicas") pointed at the same Redis. from_settings'
        # url->client wiring is unit-tested separately; here we inject the shared
        # fake client to drive the full middleware->limiter->Redis path.
        from skrift.config import RedisConfig

        replica_a = _build_app(
            settings, RateLimiter(redis_client=client_redis, redis_config=RedisConfig())
        )
        replica_b = _build_app(
            settings, RateLimiter(redis_client=client_redis, redis_config=RedisConfig())
        )
        with TestClient(replica_a) as ca, TestClient(replica_b) as cb:
            # First hit on replica A consumes the single per-minute slot...
            assert ca.post("/book/inquiry").status_code == 201
            # ...so replica B (same client IP, same Redis) is blocked.
            assert cb.post("/book/inquiry").status_code == 429

    @pytest.mark.asyncio
    async def test_all_limiters_share_the_one_backend(self):
        """Setting redis.url must make EVERY limiter distributed — the
        middleware's check and the failed-auth tracker write to the same Redis,
        with no silent in-memory divergence."""
        client_redis = fake_aioredis.FakeRedis()
        from skrift.config import RedisConfig

        limiter = RateLimiter(redis_client=client_redis, redis_config=RedisConfig())
        failed_auth = FailedAuthLimiter(counter=limiter.get_counter(60.0, "failed_auth"))

        await limiter.check("default", "1.2.3.4", [(5, "minute")])
        await failed_auth.record_failure("1.2.3.4")

        keys = [k.decode() if isinstance(k, bytes) else k for k in await client_redis.keys("*")]
        assert any(k.startswith("skrift:ratelimit:default:") for k in keys), keys
        assert any(k.startswith("skrift:ratelimit:failed_auth:") for k in keys), keys
        await client_redis.aclose()
