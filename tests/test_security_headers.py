"""Tests for security headers middleware and configuration."""

import pytest

from skrift.config import SecurityHeadersConfig
from skrift.middleware.security import SecurityHeadersMiddleware


class TestSecurityHeadersConfig:
    """Tests for SecurityHeadersConfig model."""

    def test_default_build_headers_returns_all_headers(self):
        """Default config builds all expected headers (non-debug)."""
        config = SecurityHeadersConfig()
        headers = config.build_headers(debug=False)
        names = {name for name, _ in headers}
        assert b"content-security-policy" in names
        assert b"strict-transport-security" in names
        assert b"x-content-type-options" in names
        assert b"x-frame-options" in names
        assert b"referrer-policy" in names
        assert b"permissions-policy" in names
        assert b"cross-origin-opener-policy" in names

    def test_hsts_excluded_in_debug_mode(self):
        """HSTS header is not included when debug=True."""
        config = SecurityHeadersConfig()
        headers = config.build_headers(debug=True)
        names = {name for name, _ in headers}
        assert b"strict-transport-security" not in names

    def test_hsts_included_in_production(self):
        """HSTS header is included when debug=False."""
        config = SecurityHeadersConfig()
        headers = config.build_headers(debug=False)
        names = {name for name, _ in headers}
        assert b"strict-transport-security" in names

    def test_none_header_excluded(self):
        """Setting a header to None excludes it from output."""
        config = SecurityHeadersConfig(content_security_policy=None)
        headers = config.build_headers(debug=False)
        names = {name for name, _ in headers}
        assert b"content-security-policy" not in names

    def test_empty_string_header_excluded(self):
        """Setting a header to empty string excludes it from output."""
        config = SecurityHeadersConfig(content_security_policy="")
        headers = config.build_headers(debug=False)
        names = {name for name, _ in headers}
        assert b"content-security-policy" not in names

    def test_custom_header_values(self):
        """Custom header values are used correctly."""
        config = SecurityHeadersConfig(
            x_frame_options="SAMEORIGIN",
            referrer_policy="no-referrer",
        )
        headers = config.build_headers(debug=False)
        header_dict = dict(headers)
        assert header_dict[b"x-frame-options"] == b"SAMEORIGIN"
        assert header_dict[b"referrer-policy"] == b"no-referrer"

    def test_headers_are_bytes(self):
        """All returned headers are bytes tuples."""
        config = SecurityHeadersConfig()
        headers = config.build_headers(debug=False)
        for name, value in headers:
            assert isinstance(name, bytes)
            assert isinstance(value, bytes)

    def test_all_headers_disabled_returns_empty(self):
        """Setting all headers to None returns empty list."""
        config = SecurityHeadersConfig(
            content_security_policy=None,
            strict_transport_security=None,
            x_content_type_options=None,
            x_frame_options=None,
            referrer_policy=None,
            permissions_policy=None,
            cross_origin_opener_policy=None,
        )
        headers = config.build_headers(debug=False)
        assert headers == []


class TestSecurityHeadersMiddleware:
    """Tests for SecurityHeadersMiddleware ASGI middleware."""

    @pytest.fixture
    def headers(self):
        """Standard test headers."""
        return [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
        ]

    @pytest.fixture
    def captured_messages(self):
        """List to capture sent ASGI messages."""
        return []

    def _make_send(self, captured):
        async def send(message):
            captured.append(message)
        return send

    @pytest.mark.asyncio
    async def test_injects_headers_into_response(self, headers, captured_messages):
        """Middleware adds security headers to HTTP responses."""
        async def app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html")],
            })
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = SecurityHeadersMiddleware(app, headers=headers)
        scope = {"type": "http", "method": "GET", "path": "/"}

        await middleware(scope, None, self._make_send(captured_messages))

        response_start = captured_messages[0]
        header_dict = dict(response_start["headers"])
        assert header_dict[b"x-content-type-options"] == b"nosniff"
        assert header_dict[b"x-frame-options"] == b"DENY"
        assert header_dict[b"content-type"] == b"text/html"

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_headers(self, captured_messages):
        """Middleware does not overwrite headers already set by the route."""
        security_headers = [
            (b"x-frame-options", b"DENY"),
        ]

        async def app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-frame-options", b"SAMEORIGIN")],
            })
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = SecurityHeadersMiddleware(app, headers=security_headers)
        scope = {"type": "http", "method": "GET", "path": "/"}

        await middleware(scope, None, self._make_send(captured_messages))

        response_start = captured_messages[0]
        header_dict = dict(response_start["headers"])
        # Route's value should be preserved, not overwritten
        assert header_dict[b"x-frame-options"] == b"SAMEORIGIN"

    @pytest.mark.asyncio
    async def test_passes_through_non_http_scopes(self, headers, captured_messages):
        """Non-HTTP scopes (websocket, lifespan) are passed through unchanged."""
        called = False

        async def app(scope, receive, send):
            nonlocal called
            called = True

        middleware = SecurityHeadersMiddleware(app, headers=headers)
        scope = {"type": "websocket"}

        await middleware(scope, None, None)
        assert called

    @pytest.mark.asyncio
    async def test_passes_through_body_messages(self, headers, captured_messages):
        """Body messages are passed through without modification."""
        async def app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b"hello"})

        middleware = SecurityHeadersMiddleware(app, headers=headers)
        scope = {"type": "http", "method": "GET", "path": "/"}

        await middleware(scope, None, self._make_send(captured_messages))

        body_msg = captured_messages[1]
        assert body_msg["type"] == "http.response.body"
        assert body_msg["body"] == b"hello"

    @pytest.mark.asyncio
    async def test_case_insensitive_header_matching(self, captured_messages):
        """Header matching is case-insensitive (per HTTP spec)."""
        security_headers = [
            (b"X-Frame-Options", b"DENY"),
        ]

        async def app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-frame-options", b"SAMEORIGIN")],
            })
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = SecurityHeadersMiddleware(app, headers=security_headers)
        scope = {"type": "http", "method": "GET", "path": "/"}

        await middleware(scope, None, self._make_send(captured_messages))

        response_start = captured_messages[0]
        # Should only have the original header, not the middleware one
        frame_headers = [
            v for k, v in response_start["headers"]
            if k.lower() == b"x-frame-options"
        ]
        assert len(frame_headers) == 1
        assert frame_headers[0] == b"SAMEORIGIN"

    @pytest.mark.asyncio
    async def test_empty_headers_list(self, captured_messages):
        """Empty headers list doesn't break anything."""
        async def app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html")],
            })
            await send({"type": "http.response.body", "body": b"OK"})

        middleware = SecurityHeadersMiddleware(app, headers=[])
        scope = {"type": "http", "method": "GET", "path": "/"}

        await middleware(scope, None, self._make_send(captured_messages))

        response_start = captured_messages[0]
        assert len(response_start["headers"]) == 1


class TestSecurityHeadersConfigFromYAML:
    """Tests for loading SecurityHeadersConfig from YAML-like dicts."""

    def test_from_empty_dict(self):
        """Empty dict uses all defaults."""
        config = SecurityHeadersConfig(**{})
        assert config.enabled is True
        assert config.x_content_type_options == "nosniff"

    def test_partial_override(self):
        """Partial dict overrides only specified fields."""
        config = SecurityHeadersConfig(**{
            "x_frame_options": "SAMEORIGIN",
            "content_security_policy": None,
        })
        assert config.x_frame_options == "SAMEORIGIN"
        assert config.content_security_policy is None
        assert config.x_content_type_options == "nosniff"  # default preserved

    def test_disabled_config(self):
        """enabled=False disables the middleware."""
        config = SecurityHeadersConfig(**{"enabled": False})
        assert config.enabled is False
