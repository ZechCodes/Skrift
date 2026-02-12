"""Tests for skrift.app_config module."""

import hashlib
from unittest.mock import MagicMock

import pytest

from skrift.app_config import (
    build_db_config,
    build_session_config,
    build_security_middleware,
    build_rate_limit_middleware,
)


def _make_settings(**overrides):
    """Create a mock Settings object with sensible defaults."""
    s = MagicMock()
    s.debug = overrides.pop("debug", False)
    s.secret_key = overrides.pop("secret_key", "test-secret-key")

    db = MagicMock()
    db.url = "sqlite+aiosqlite:///./test.db"
    db.echo = False
    db.pool_size = 5
    db.pool_overflow = 10
    db.pool_timeout = 30
    db.pool_pre_ping = True
    s.db = db

    session = MagicMock()
    session.max_age = 86400
    session.cookie_domain = None
    s.session = session

    security_headers = MagicMock()
    security_headers.enabled = True
    security_headers.content_security_policy = "default-src 'self'"
    security_headers.csp_nonce = True
    s.security_headers = security_headers

    rate_limit = MagicMock()
    rate_limit.enabled = True
    rate_limit.requests_per_minute = 60
    rate_limit.auth_requests_per_minute = 10
    rate_limit.paths = {}
    s.rate_limit = rate_limit

    return s


# ---------------------------------------------------------------------------
# build_db_config
# ---------------------------------------------------------------------------

class TestBuildDbConfig:
    def test_sqlite_url_connection_string(self):
        settings = _make_settings()
        settings.db.url = "sqlite+aiosqlite:///./test.db"
        settings.db.echo = True

        config = build_db_config(settings)

        assert config.connection_string == "sqlite+aiosqlite:///./test.db"
        assert config.engine_config.echo is True

    def test_sqlite_url_does_not_set_pool_size(self):
        """SQLite EngineConfig is created without explicit pool params."""
        from advanced_alchemy.utils.dataclass import Empty

        settings = _make_settings()
        settings.db.url = "sqlite+aiosqlite:///./test.db"

        config = build_db_config(settings)

        # Pool size should remain at its default (Empty sentinel) for sqlite
        assert config.engine_config.pool_size is Empty

    def test_non_sqlite_url_sets_pool_params(self):
        settings = _make_settings()
        settings.db.url = "postgresql+asyncpg://user:pass@localhost/mydb"
        settings.db.echo = False
        settings.db.pool_size = 10
        settings.db.pool_overflow = 20
        settings.db.pool_timeout = 60
        settings.db.pool_pre_ping = False

        config = build_db_config(settings)

        assert config.connection_string == "postgresql+asyncpg://user:pass@localhost/mydb"
        assert config.engine_config.echo is False
        assert config.engine_config.pool_size == 10
        assert config.engine_config.max_overflow == 20
        assert config.engine_config.pool_timeout == 60
        assert config.engine_config.pool_pre_ping is False

    def test_connection_string_matches_settings_url(self):
        url = "mysql+aiomysql://root@localhost/test"
        settings = _make_settings()
        settings.db.url = url

        config = build_db_config(settings)

        assert config.connection_string == url


# ---------------------------------------------------------------------------
# build_session_config
# ---------------------------------------------------------------------------

class TestBuildSessionConfig:
    def test_secret_is_sha256_of_secret_key(self):
        secret_key = "my-super-secret"
        settings = _make_settings(secret_key=secret_key)

        config = build_session_config(settings)

        expected = hashlib.sha256(secret_key.encode()).digest()
        assert config.secret == expected

    def test_max_age_matches_session_config(self):
        settings = _make_settings()
        settings.session.max_age = 3600

        config = build_session_config(settings)

        assert config.max_age == 3600

    def test_secure_true_when_not_debug(self):
        settings = _make_settings(debug=False)

        config = build_session_config(settings)

        assert config.secure is True

    def test_secure_false_when_debug(self):
        settings = _make_settings(debug=True)

        config = build_session_config(settings)

        assert config.secure is False

    def test_samesite_is_lax(self):
        settings = _make_settings()

        config = build_session_config(settings)

        assert config.samesite == "lax"


# ---------------------------------------------------------------------------
# build_security_middleware
# ---------------------------------------------------------------------------

class TestBuildSecurityMiddleware:
    def test_enabled_returns_one_middleware(self):
        settings = _make_settings()
        settings.security_headers.enabled = True
        settings.security_headers.build_headers.return_value = [
            (b"x-frame-options", b"DENY"),
        ]

        result = build_security_middleware(settings)
        assert len(result) == 1

    def test_disabled_returns_empty(self):
        settings = _make_settings()
        settings.security_headers.enabled = False

        result = build_security_middleware(settings)
        assert len(result) == 0

    def test_no_headers_and_no_csp_returns_empty(self):
        settings = _make_settings()
        settings.security_headers.enabled = True
        settings.security_headers.build_headers.return_value = []
        settings.security_headers.content_security_policy = None

        result = build_security_middleware(settings)
        assert len(result) == 0

    def test_no_headers_but_csp_returns_one(self):
        settings = _make_settings()
        settings.security_headers.enabled = True
        settings.security_headers.build_headers.return_value = []
        settings.security_headers.content_security_policy = "default-src 'self'"

        result = build_security_middleware(settings)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# build_rate_limit_middleware
# ---------------------------------------------------------------------------

class TestBuildRateLimitMiddleware:
    def test_enabled_returns_one_middleware(self):
        settings = _make_settings()
        settings.rate_limit.enabled = True

        result = build_rate_limit_middleware(settings)
        assert len(result) == 1

    def test_disabled_returns_empty(self):
        settings = _make_settings()
        settings.rate_limit.enabled = False

        result = build_rate_limit_middleware(settings)
        assert len(result) == 0
