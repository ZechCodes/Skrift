"""Tests for hostname-scoped session cookie cleanup.

When cookie_domain is configured (e.g. .example.com), session cookies
previously set without a domain (scoped to the exact hostname) can shadow
the domain cookie.  The custom session backend always emits a clear
cookie without a Domain attribute to expire any hostname-scoped cookie.
"""

import hashlib
import time
from base64 import b64encode
from os import urandom
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from litestar.middleware.session.client_side import AAD, NONCE_SIZE
from litestar.serialization import encode_json

from skrift.app_factory import _SessionBackend, _SessionConfig


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


class TestHostnameCookieCleanup:
    """Tests for _SessionBackend.store_in_message."""

    @pytest.mark.asyncio
    async def test_clears_hostname_cookie_when_domain_configured(self):
        """When domain is configured and a session cookie is present,
        a clear cookie without Domain should be emitted."""
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

        # Should have domain-scoped cookie AND a hostname clear
        domain_cookies = [c for c in set_cookies if "domain=" in c.lower()]
        no_domain_clears = [
            c for c in set_cookies
            if "domain=" not in c.lower() and "null" in c.lower()
        ]

        assert len(domain_cookies) >= 1, f"Expected domain cookie, got: {set_cookies}"
        assert len(no_domain_clears) >= 1, f"Expected no-domain clear, got: {set_cookies}"

    @pytest.mark.asyncio
    async def test_no_clear_when_no_domain_configured(self):
        """When cookie_domain is None, no extra clear is needed."""
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret, domain=None)
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

        # No hostname clear should be emitted
        null_no_domain = [
            c for c in set_cookies
            if "domain=" not in c.lower() and "null" in c.lower()
        ]
        assert len(null_no_domain) == 0, f"Unexpected hostname clear: {null_no_domain}"

    @pytest.mark.asyncio
    async def test_no_clear_when_no_cookie_in_request(self):
        """When no session cookie was in the request, no clear is needed."""
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret, domain=".example.com")
        backend = _SessionBackend(config)

        message = {"type": "http.response.start", "headers": []}
        conn = _make_connection({})  # No cookies in request

        await backend.store_in_message({"user_id": "123"}, message, conn)

        set_cookies = [
            v.decode() if isinstance(v, bytes) else v
            for k, v in message["headers"]
            if (k.decode() if isinstance(k, bytes) else k).lower() == "set-cookie"
        ]

        # Should set the domain cookie but no hostname clear
        null_no_domain = [
            c for c in set_cookies
            if "domain=" not in c.lower() and "null" in c.lower()
        ]
        assert len(null_no_domain) == 0, f"Unexpected hostname clear: {null_no_domain}"

    @pytest.mark.asyncio
    async def test_clears_on_logout(self):
        """On logout (empty session), hostname cookie should also be cleared."""
        secret = hashlib.sha256(b"test-key").digest()
        config = _make_config(secret, domain=".example.com")
        backend = _SessionBackend(config)

        cookie_value = _encrypt_session(secret, {"user_id": "123"})
        message = {"type": "http.response.start", "headers": []}
        conn = _make_connection({"session": cookie_value})

        # Empty session = logout
        await backend.store_in_message({}, message, conn)

        set_cookies = [
            v.decode() if isinstance(v, bytes) else v
            for k, v in message["headers"]
            if (k.decode() if isinstance(k, bytes) else k).lower() == "set-cookie"
        ]

        # Should have both domain clear and hostname clear
        no_domain_clears = [
            c for c in set_cookies
            if "domain=" not in c.lower() and "null" in c.lower()
        ]
        assert len(no_domain_clears) >= 1, f"Expected hostname clear on logout, got: {set_cookies}"


class TestSessionConfigUsesCustomBackend:
    """Verify _SessionConfig wires up the custom backend."""

    def test_backend_class(self):
        config = _make_config(hashlib.sha256(b"test").digest())
        assert config._backend_class is _SessionBackend

    def test_middleware_creates_custom_backend(self):
        config = _make_config(hashlib.sha256(b"test").digest())
        middleware_def = config.middleware
        assert middleware_def.kwargs["backend"].__class__ is _SessionBackend
