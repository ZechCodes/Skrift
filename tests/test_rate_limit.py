"""Tests for rate limiting middleware."""

import pytest
from litestar import Litestar, get
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient

from skrift.config import RateLimitConfig
from skrift.middleware.rate_limit import RateLimitMiddleware


class TestRateLimitConfig:
    """Tests for RateLimitConfig model."""

    def test_defaults(self):
        config = RateLimitConfig()
        assert config.enabled is True
        assert config.requests_per_minute == 60
        assert config.auth_requests_per_minute == 10
        assert config.paths == {}

    def test_custom_values(self):
        config = RateLimitConfig(
            requests_per_minute=120,
            auth_requests_per_minute=20,
            paths={"/api": 200},
        )
        assert config.requests_per_minute == 120
        assert config.auth_requests_per_minute == 20
        assert config.paths == {"/api": 200}


class TestRateLimitMiddleware:
    """Tests for RateLimitMiddleware ASGI middleware."""

    @pytest.fixture
    def captured_messages(self):
        return []

    def _make_send(self, captured):
        async def send(message):
            captured.append(message)
        return send

    def _make_app(self):
        async def app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({"type": "http.response.body", "body": b"OK"})
        return app

    def _make_scope(self, path="/", client_ip="127.0.0.1"):
        return {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "client": (client_ip, 0),
        }

    @pytest.mark.asyncio
    async def test_under_limit_passes_through(self, captured_messages):
        """Requests under the limit pass through normally."""
        middleware = RateLimitMiddleware(
            self._make_app(), requests_per_minute=5
        )
        scope = self._make_scope()

        await middleware(scope, None, self._make_send(captured_messages))

        assert captured_messages[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_over_limit_returns_429(self):
        """Requests over the limit return 429."""
        middleware = RateLimitMiddleware(
            self._make_app(), requests_per_minute=3
        )

        for _ in range(3):
            captured = []
            scope = self._make_scope()
            await middleware(scope, None, self._make_send(captured))
            assert captured[0]["status"] == 200

        # 4th request should be rejected
        captured = []
        scope = self._make_scope()
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 429

    @pytest.mark.asyncio
    async def test_429_includes_retry_after(self):
        """429 responses include Retry-After header."""
        middleware = RateLimitMiddleware(
            self._make_app(), requests_per_minute=1
        )

        # First request
        captured = []
        scope = self._make_scope()
        await middleware(scope, None, self._make_send(captured))

        # Second request should be rejected with Retry-After
        captured = []
        scope = self._make_scope()
        await middleware(scope, None, self._make_send(captured))

        assert captured[0]["status"] == 429
        header_dict = dict(captured[0]["headers"])
        assert b"retry-after" in header_dict
        retry_after = int(header_dict[b"retry-after"])
        assert retry_after > 0

    @pytest.mark.asyncio
    async def test_auth_path_uses_stricter_limit(self):
        """Auth paths use auth_requests_per_minute limit."""
        middleware = RateLimitMiddleware(
            self._make_app(),
            requests_per_minute=100,
            auth_requests_per_minute=2,
        )

        # 2 auth requests should succeed
        for _ in range(2):
            captured = []
            scope = self._make_scope(path="/auth/login")
            await middleware(scope, None, self._make_send(captured))
            assert captured[0]["status"] == 200

        # 3rd auth request should be rejected
        captured = []
        scope = self._make_scope(path="/auth/login")
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 429

    @pytest.mark.asyncio
    async def test_per_ip_isolation(self):
        """Different IPs have independent rate limits."""
        middleware = RateLimitMiddleware(
            self._make_app(), requests_per_minute=2
        )

        # 2 requests from IP A
        for _ in range(2):
            captured = []
            scope = self._make_scope(client_ip="10.0.0.1")
            await middleware(scope, None, self._make_send(captured))
            assert captured[0]["status"] == 200

        # IP A should now be limited
        captured = []
        scope = self._make_scope(client_ip="10.0.0.1")
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 429

        # IP B should still be allowed
        captured = []
        scope = self._make_scope(client_ip="10.0.0.2")
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_custom_path_limits(self):
        """Custom path prefix overrides use their own limits."""
        middleware = RateLimitMiddleware(
            self._make_app(),
            requests_per_minute=100,
            paths={"/api": 2},
        )

        # 2 API requests should succeed
        for _ in range(2):
            captured = []
            scope = self._make_scope(path="/api/data")
            await middleware(scope, None, self._make_send(captured))
            assert captured[0]["status"] == 200

        # 3rd API request should be rejected
        captured = []
        scope = self._make_scope(path="/api/data")
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 429

        # Non-API request should still work
        captured = []
        scope = self._make_scope(path="/home")
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_non_http_passthrough(self):
        """Non-HTTP scopes are passed through unchanged."""
        called = False

        async def app(scope, receive, send):
            nonlocal called
            called = True

        middleware = RateLimitMiddleware(app)
        scope = {"type": "websocket"}

        await middleware(scope, None, None)
        assert called

    @pytest.mark.asyncio
    async def test_x_forwarded_for_header(self):
        """Uses x-forwarded-for header for IP when present."""
        middleware = RateLimitMiddleware(
            self._make_app(), requests_per_minute=1
        )

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"192.168.1.1, 10.0.0.1")],
            "client": ("127.0.0.1", 0),
        }

        # First request from forwarded IP
        captured = []
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 200

        # Second request from same forwarded IP should be blocked
        captured = []
        await middleware(scope, None, self._make_send(captured))
        assert captured[0]["status"] == 429

        # But a different forwarded IP should pass
        scope2 = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"192.168.1.2")],
            "client": ("127.0.0.1", 0),
        }
        captured = []
        await middleware(scope2, None, self._make_send(captured))
        assert captured[0]["status"] == 200


class TestRateLimitIntegration:
    """Integration tests using Litestar's TestClient to verify middleware
    engages properly in the real Litestar pipeline."""

    def _create_app(self, auth_limit: int = 3, general_limit: int = 60) -> Litestar:
        @get("/auth/test")
        async def auth_handler() -> str:
            return "ok"

        @get("/public/test")
        async def public_handler() -> str:
            return "ok"

        return Litestar(
            route_handlers=[auth_handler, public_handler],
            middleware=[
                DefineMiddleware(
                    RateLimitMiddleware,
                    requests_per_minute=general_limit,
                    auth_requests_per_minute=auth_limit,
                )
            ],
        )

    def test_auth_rate_limit_triggers_429(self):
        """Auth endpoints return 429 after exceeding auth_requests_per_minute."""
        app = self._create_app(auth_limit=3)
        with TestClient(app) as client:
            for i in range(3):
                resp = client.get("/auth/test")
                assert resp.status_code == 200, f"Request {i+1} should pass"

            resp = client.get("/auth/test")
            assert resp.status_code == 429
            assert "retry-after" in resp.headers

    def test_general_rate_limit_triggers_429(self):
        """Non-auth endpoints return 429 after exceeding requests_per_minute."""
        app = self._create_app(general_limit=3)
        with TestClient(app) as client:
            for i in range(3):
                resp = client.get("/public/test")
                assert resp.status_code == 200, f"Request {i+1} should pass"

            resp = client.get("/public/test")
            assert resp.status_code == 429

    def test_auth_limit_independent_of_general(self):
        """Auth limit doesn't consume the general bucket and vice versa."""
        app = self._create_app(auth_limit=2, general_limit=100)
        with TestClient(app) as client:
            # Exhaust auth limit
            for _ in range(2):
                resp = client.get("/auth/test")
                assert resp.status_code == 200

            resp = client.get("/auth/test")
            assert resp.status_code == 429

            # General path should still work
            resp = client.get("/public/test")
            assert resp.status_code == 200

