"""Tests for stale session cookie cleanup.

When cookie_domain is configured (e.g. .example.com), session cookies
previously set without a domain (scoped to the exact hostname) can shadow
the domain cookie.  The custom session backend in app_factory detects
undecryptable cookies and expires them on the exact hostname.
"""

import hashlib
import time
from base64 import b64encode
from os import urandom
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from litestar.datastructures.cookie import Cookie
from litestar.middleware.session.client_side import AAD, NONCE_SIZE
from litestar.serialization import encode_json

from skrift.app_factory import _SessionBackend, _SessionConfig, _STALE_SESSION_KEY


def _make_config(secret: bytes, domain: str | None = ".example.com"):
    return _SessionConfig(
        secret=secret,
        key="session",
        max_age=86400,
        httponly=True,
        secure=True,
        samesite="lax",
        domain=domain,
    )


def _encrypt_session(secret: bytes, data: dict) -> str:
    """Encrypt session data the same way Litestar does."""
    aesgcm = AESGCM(secret)
    nonce = urandom(NONCE_SIZE)
    aad_data = encode_json({"expires_at": round(time.time()) + 86400})
    encrypted = aesgcm.encrypt(nonce, encode_json(data), associated_data=aad_data)
    raw = nonce + encrypted + AAD + aad_data
    return b64encode(raw).decode("utf-8")


def _make_connection(cookies: dict, scope_extras: dict | None = None):
    conn = MagicMock()
    conn.cookies = cookies
    conn.scope = scope_extras or {}
    return conn


class TestStaleSessionDetection:
    """Tests for _SessionBackend.load_from_connection."""

    @pytest.mark.asyncio
    async def test_valid_cookie_loads_normally(self):
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret)
        backend = _SessionBackend(config)

        cookie_value = _encrypt_session(secret, {"user_id": "123"})
        conn = _make_connection({"session": cookie_value})

        result = await backend.load_from_connection(conn)

        assert result == {"user_id": "123"}
        assert _STALE_SESSION_KEY not in conn.scope

    @pytest.mark.asyncio
    async def test_corrupt_cookie_flags_stale(self):
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret)
        backend = _SessionBackend(config)

        conn = _make_connection({"session": "totally-corrupt-garbage-data!!"})

        result = await backend.load_from_connection(conn)

        assert result == {}
        assert conn.scope.get(_STALE_SESSION_KEY) is True

    @pytest.mark.asyncio
    async def test_wrong_key_cookie_flags_stale(self):
        old_secret = hashlib.sha256(b"old-key").digest()
        new_secret = hashlib.sha256(b"new-key").digest()
        config = _make_config(new_secret)
        backend = _SessionBackend(config)

        cookie_value = _encrypt_session(old_secret, {"user_id": "123"})
        conn = _make_connection({"session": cookie_value})

        result = await backend.load_from_connection(conn)

        assert result == {}
        assert conn.scope.get(_STALE_SESSION_KEY) is True

    @pytest.mark.asyncio
    async def test_no_cookie_no_flag(self):
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret)
        backend = _SessionBackend(config)

        conn = _make_connection({})

        result = await backend.load_from_connection(conn)

        assert result == {}
        assert _STALE_SESSION_KEY not in conn.scope


class TestStaleCookieCleanup:
    """Tests for _SessionBackend.store_in_message."""

    @pytest.mark.asyncio
    async def test_stale_cookie_cleared_without_domain(self):
        """When a stale cookie is detected, a clear cookie without Domain should be added."""
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret, domain=".example.com")
        backend = _SessionBackend(config)

        message = {"type": "http.response.start", "headers": []}
        conn = _make_connection(
            {"session": "corrupt"},
            scope_extras={_STALE_SESSION_KEY: True},
        )

        # Store empty session (like what happens after failed decryption)
        await backend.store_in_message({}, message, conn)

        # Parse out all Set-Cookie headers
        set_cookies = [
            v.decode() if isinstance(v, bytes) else v
            for k, v in message["headers"]
            if (k.decode() if isinstance(k, bytes) else k).lower() == "set-cookie"
        ]

        # Should have at least two Set-Cookie headers:
        # 1. The normal clear with Domain=.example.com (from parent class)
        # 2. The hostname-scoped clear without Domain (from our override)
        domain_clears = [c for c in set_cookies if "domain=" in c.lower()]
        no_domain_clears = [c for c in set_cookies if "domain=" not in c.lower()]

        assert len(domain_clears) >= 1, f"Expected domain clear cookie, got: {set_cookies}"
        assert len(no_domain_clears) >= 1, f"Expected no-domain clear cookie, got: {set_cookies}"

        # The no-domain clear should expire the cookie
        assert any("expires=" in c.lower() or "max-age=0" in c.lower() for c in no_domain_clears)

    @pytest.mark.asyncio
    async def test_no_extra_clear_when_cookie_is_valid(self):
        """When the cookie decrypts fine, no extra clear headers should be added."""
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret, domain=".example.com")
        backend = _SessionBackend(config)

        cookie_value = _encrypt_session(secret, {"user_id": "123"})
        message = {"type": "http.response.start", "headers": []}
        conn = _make_connection({"session": cookie_value})

        await backend.store_in_message({"user_id": "123"}, message, conn)

        set_cookies = [
            v.decode() if isinstance(v, bytes) else v
            for k, v in message["headers"]
            if (k.decode() if isinstance(k, bytes) else k).lower() == "set-cookie"
        ]

        # All set-cookie headers should have the configured domain
        no_domain_clears = [
            c for c in set_cookies
            if "session" in c.lower() and "domain=" not in c.lower() and "null" in c.lower()
        ]
        assert len(no_domain_clears) == 0, f"Unexpected no-domain clear: {no_domain_clears}"

    @pytest.mark.asyncio
    async def test_no_extra_clear_when_no_domain_configured(self):
        """When cookie_domain is None, no extra clear is needed."""
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret, domain=None)
        backend = _SessionBackend(config)

        message = {"type": "http.response.start", "headers": []}
        conn = _make_connection(
            {"session": "corrupt"},
            scope_extras={_STALE_SESSION_KEY: True},
        )

        await backend.store_in_message({}, message, conn)

        set_cookies = [
            v.decode() if isinstance(v, bytes) else v
            for k, v in message["headers"]
            if (k.decode() if isinstance(k, bytes) else k).lower() == "set-cookie"
        ]

        # Should only have the normal clears (without domain since none configured)
        # No EXTRA clears from our override
        null_cookies = [c for c in set_cookies if "null" in c.lower()]
        # The parent class will emit one clear — we should not add another
        assert len(null_cookies) <= 1

    @pytest.mark.asyncio
    async def test_flag_is_consumed(self):
        """The stale session flag should be popped (consumed) after store_in_message."""
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret, domain=".example.com")
        backend = _SessionBackend(config)

        message = {"type": "http.response.start", "headers": []}
        conn = _make_connection(
            {"session": "corrupt"},
            scope_extras={_STALE_SESSION_KEY: True},
        )

        await backend.store_in_message({}, message, conn)

        assert _STALE_SESSION_KEY not in conn.scope


class TestSessionConfigUsesCustomBackend:
    """Verify _SessionConfig wires up the custom backend."""

    def test_backend_class(self):
        config = _make_config(hashlib.sha256(b"test").digest())
        assert config._backend_class is _SessionBackend

    def test_middleware_creates_custom_backend(self):
        config = _make_config(hashlib.sha256(b"test").digest())
        middleware_def = config.middleware
        # The DefineMiddleware kwargs should reference our backend
        assert middleware_def.kwargs["backend"].__class__ is _SessionBackend
