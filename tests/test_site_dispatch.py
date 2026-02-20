"""Tests for subdomain site dispatch middleware."""

import pytest

from skrift.middleware.site_dispatch import SiteDispatcher, _extract_host, _get_subdomain


class TestExtractHost:
    def test_extracts_host_from_headers(self):
        scope = {"headers": [(b"host", b"example.com")]}
        assert _extract_host(scope) == "example.com"

    def test_strips_port(self):
        scope = {"headers": [(b"host", b"example.com:8000")]}
        assert _extract_host(scope) == "example.com"

    def test_lowercases(self):
        scope = {"headers": [(b"host", b"Example.COM")]}
        assert _extract_host(scope) == "example.com"

    def test_empty_when_no_host_header(self):
        scope = {"headers": [(b"accept", b"text/html")]}
        assert _extract_host(scope) == ""

    def test_empty_when_no_headers(self):
        scope = {}
        assert _extract_host(scope) == ""


class TestGetSubdomain:
    def test_returns_subdomain(self):
        assert _get_subdomain("blog.example.com", "example.com") == "blog"

    def test_returns_empty_for_primary(self):
        assert _get_subdomain("example.com", "example.com") == ""

    def test_returns_empty_for_unrelated(self):
        assert _get_subdomain("other.net", "example.com") == ""

    def test_case_insensitive(self):
        assert _get_subdomain("BLOG.example.com", "Example.COM") == "blog"

    def test_nested_subdomain(self):
        assert _get_subdomain("a.b.example.com", "example.com") == "a.b"


class TestSiteDispatcher:
    @pytest.fixture
    def captured_messages(self):
        messages = []

        async def send(msg):
            messages.append(msg)

        return messages, send

    def _make_app(self, name):
        """Create a simple ASGI app that records the scope state."""
        async def app(scope, receive, send):
            if scope["type"] == "http":
                body = name.encode()
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": body})

        return app

    def _make_scope(self, host="example.com", path="/"):
        return {
            "type": "http",
            "path": path,
            "headers": [(b"host", host.encode())],
        }

    @pytest.mark.asyncio
    async def test_routes_to_primary_for_primary_domain(self, captured_messages):
        messages, send = captured_messages
        primary = self._make_app("primary")
        dispatcher = SiteDispatcher(primary, {"blog": self._make_app("blog")}, "example.com")

        scope = self._make_scope("example.com")
        await dispatcher(scope, None, send)

        assert messages[1]["body"] == b"primary"
        assert scope["state"]["site_name"] == ""

    @pytest.mark.asyncio
    async def test_routes_to_site_app_for_subdomain(self, captured_messages):
        messages, send = captured_messages
        primary = self._make_app("primary")
        blog = self._make_app("blog")
        dispatcher = SiteDispatcher(primary, {"blog": blog}, "example.com")

        scope = self._make_scope("blog.example.com")
        await dispatcher(scope, None, send)

        assert messages[1]["body"] == b"blog"
        assert scope["state"]["site_name"] == "blog"

    @pytest.mark.asyncio
    async def test_falls_back_to_primary_for_unknown_subdomain(self, captured_messages):
        messages, send = captured_messages
        primary = self._make_app("primary")
        dispatcher = SiteDispatcher(primary, {"blog": self._make_app("blog")}, "example.com")

        scope = self._make_scope("unknown.example.com")
        await dispatcher(scope, None, send)

        assert messages[1]["body"] == b"primary"
        assert scope["state"]["site_name"] == ""

    @pytest.mark.asyncio
    async def test_non_http_goes_to_primary(self):
        called_with = {}

        async def primary(scope, receive, send):
            called_with["type"] = scope["type"]

        dispatcher = SiteDispatcher(primary, {}, "example.com")
        scope = {"type": "websocket", "headers": [(b"host", b"blog.example.com")]}
        await dispatcher(scope, None, None)

        assert called_with["type"] == "websocket"

    @pytest.mark.asyncio
    async def test_sets_state_dict_if_missing(self, captured_messages):
        messages, send = captured_messages
        primary = self._make_app("primary")
        dispatcher = SiteDispatcher(primary, {}, "example.com")

        scope = self._make_scope("example.com")
        # state not in scope initially
        assert "state" not in scope
        await dispatcher(scope, None, send)
        assert "state" in scope

    @pytest.mark.asyncio
    async def test_preserves_existing_state(self, captured_messages):
        messages, send = captured_messages
        primary = self._make_app("primary")
        dispatcher = SiteDispatcher(primary, {}, "example.com")

        scope = self._make_scope("example.com")
        scope["state"] = {"existing_key": "value"}
        await dispatcher(scope, None, send)

        assert scope["state"]["existing_key"] == "value"
        assert scope["state"]["site_name"] == ""

    @pytest.mark.asyncio
    async def test_host_with_port(self, captured_messages):
        messages, send = captured_messages
        blog = self._make_app("blog")
        dispatcher = SiteDispatcher(self._make_app("primary"), {"blog": blog}, "example.com")

        scope = self._make_scope("blog.example.com:8000")
        await dispatcher(scope, None, send)

        assert messages[1]["body"] == b"blog"
