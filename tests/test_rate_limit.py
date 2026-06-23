"""Tests for rate limiting middleware."""

import pytest
from litestar import Litestar, get
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient

from skrift.config import (
    RateLimitConfig,
    RateLimitMatch,
    RateLimitRule,
    RateLimitWindow,
)
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


class TestRateLimitConfigResolve:
    """The config compiles legacy + declarative settings into a single rule
    per request via resolve(path, method)."""

    def test_default_window_from_legacy(self):
        config = RateLimitConfig(requests_per_minute=60)
        resolved = config.resolve("/home", "GET")
        assert resolved.name == "default"
        assert resolved.key == "ip"
        assert resolved.limits == [(60, 60.0)]

    def test_auth_window_from_legacy(self):
        config = RateLimitConfig(auth_requests_per_minute=10)
        resolved = config.resolve("/auth/login", "POST")
        assert resolved.name == "auth"
        assert resolved.limits == [(10, 60.0)]

    def test_legacy_path_prefix_wins_over_default(self):
        config = RateLimitConfig(requests_per_minute=100, paths={"/api": 2})
        resolved = config.resolve("/api/data", "GET")
        assert resolved.limits == [(2, 60.0)]
        # Non-matching path falls back to default.
        assert config.resolve("/home", "GET").limits == [(100, 60.0)]

    def test_longest_legacy_prefix_wins(self):
        config = RateLimitConfig(paths={"/api": 100, "/api/admin": 5})
        assert config.resolve("/api/admin/x", "GET").limits == [(5, 60.0)]

    def test_new_style_default_and_auth(self):
        config = RateLimitConfig(
            default={"limit": 120, "per": "minute"},
            auth={"limit": 30, "per": "minute"},
        )
        assert config.resolve("/x", "GET").limits == [(120, 60.0)]
        assert config.resolve("/auth/x", "GET").limits == [(30, 60.0)]

    def test_declarative_multi_window_rule(self):
        config = RateLimitConfig(
            rules=[
                {
                    "match": {"path": "/book/inquiry", "method": "POST"},
                    "key": "ip",
                    "limits": [
                        {"limit": 1, "per": "minute"},
                        {"limit": 6, "per": "hour"},
                    ],
                }
            ]
        )
        resolved = config.resolve("/book/inquiry", "POST")
        assert resolved.key == "ip"
        assert resolved.limits == [(1, 60.0), (6, 3600.0)]

    def test_rule_method_mismatch_falls_through_to_default(self):
        config = RateLimitConfig(
            requests_per_minute=50,
            rules=[
                {
                    "match": {"path": "/book/inquiry", "method": "POST"},
                    "limits": [{"limit": 1, "per": "minute"}],
                }
            ],
        )
        # GET doesn't match the POST-only rule.
        assert config.resolve("/book/inquiry", "GET").limits == [(50, 60.0)]

    def test_explicit_rule_outranks_legacy_path(self):
        config = RateLimitConfig(
            paths={"/api": 100},
            rules=[
                {
                    "match": {"path": "/api/expensive", "method": "POST"},
                    "limits": [{"limit": 1, "per": "minute"}],
                }
            ],
        )
        assert config.resolve("/api/expensive", "POST").limits == [(1, 60.0)]

    def test_window_seconds_escape_hatch(self):
        config = RateLimitConfig(
            rules=[
                {
                    "match": {"path": "/x"},
                    "limits": [{"limit": 3, "per": 90}],
                }
            ]
        )
        assert config.resolve("/x", "GET").limits == [(3, 90.0)]

    def test_invalid_period_rejected(self):
        import pytest as _pytest

        with _pytest.raises(Exception):
            RateLimitConfig(
                rules=[{"match": {"path": "/x"}, "limits": [{"limit": 1, "per": "week"}]}]
            )


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
    async def test_x_forwarded_for_ignored_without_trust(self):
        """Spoofed XFF from untrusted peer must not influence rate limit keys.

        Regression test for #120: previously, any client could send
        ``X-Forwarded-For`` to appear as a different IP and bypass per-IP
        limits. Now, without ClientIPMiddleware setting scope state, the
        limiter falls back to the socket peer — so spoofed XFF is ignored.
        """
        middleware = RateLimitMiddleware(
            self._make_app(), requests_per_minute=1
        )

        scope_a = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"1.2.3.4")],
            "client": ("127.0.0.1", 0),
        }
        scope_b = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"5.6.7.8")],
            "client": ("127.0.0.1", 0),
        }

        # First request passes
        captured = []
        await middleware(scope_a, None, self._make_send(captured))
        assert captured[0]["status"] == 200

        # Second request with a DIFFERENT spoofed XFF still counts against
        # the same (socket-peer) bucket — rate limit enforced.
        captured = []
        await middleware(scope_b, None, self._make_send(captured))
        assert captured[0]["status"] == 429

    @pytest.mark.asyncio
    async def test_rate_limit_keys_on_resolved_client_ip(self):
        """When ClientIPMiddleware has resolved a client IP, the limiter uses it."""
        middleware = RateLimitMiddleware(
            self._make_app(), requests_per_minute=1
        )

        def scope_with_resolved_ip(ip: str) -> dict:
            return {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "client": ("10.0.0.5", 0),
                "state": {"client_ip": ip, "client_ip_source": "xff"},
            }

        # Two requests from the same resolved IP
        captured = []
        await middleware(scope_with_resolved_ip("203.0.113.7"), None, self._make_send(captured))
        assert captured[0]["status"] == 200
        captured = []
        await middleware(scope_with_resolved_ip("203.0.113.7"), None, self._make_send(captured))
        assert captured[0]["status"] == 429

        # Different resolved IP gets its own bucket
        captured = []
        await middleware(scope_with_resolved_ip("198.51.100.9"), None, self._make_send(captured))
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


class TestDeclarativeMultiWindowRule:
    """The motivating example from issue #153: a public, anonymous lead-capture
    endpoint capped at 1/min AND 6/hr per IP — expressed in config alone."""

    def _create_app(self) -> Litestar:
        from litestar import post

        @post("/book/inquiry")
        async def inquiry() -> str:
            return "ok"

        @get("/other")
        async def other() -> str:
            return "ok"

        config = RateLimitConfig(
            requests_per_minute=1000,  # generous default
            rules=[
                RateLimitRule(
                    match=RateLimitMatch(path="/book/inquiry", method="POST"),
                    key="ip",
                    limits=[
                        RateLimitWindow(limit=1, per="minute"),
                        RateLimitWindow(limit=6, per="hour"),
                    ],
                )
            ],
        )
        return Litestar(
            route_handlers=[inquiry, other],
            middleware=[DefineMiddleware(RateLimitMiddleware, config=config)],
        )

    def test_minute_window_enforced(self):
        app = self._create_app()
        with TestClient(app) as client:
            assert client.post("/book/inquiry").status_code == 201
            resp = client.post("/book/inquiry")
            assert resp.status_code == 429
            assert "retry-after" in resp.headers

    def test_other_routes_unaffected(self):
        app = self._create_app()
        with TestClient(app) as client:
            # The default (1000/min) governs unmatched routes.
            for _ in range(5):
                assert client.get("/other").status_code == 200

