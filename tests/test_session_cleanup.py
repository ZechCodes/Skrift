"""Tests for SQLAlchemy session cleanup on request cancellation."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from skrift.config import DatabaseConfig
from skrift.db.session import SessionCleanupMiddleware


class TestDatabaseConfig:
    """Tests for DatabaseConfig pool_pre_ping setting."""

    def test_pool_pre_ping_defaults_to_true(self):
        """pool_pre_ping should default to True for connection resilience."""
        config = DatabaseConfig()
        assert config.pool_pre_ping is True

    def test_pool_pre_ping_can_be_disabled(self):
        """pool_pre_ping can be explicitly disabled."""
        config = DatabaseConfig(pool_pre_ping=False)
        assert config.pool_pre_ping is False

    def test_pool_pre_ping_can_be_enabled(self):
        """pool_pre_ping can be explicitly enabled."""
        config = DatabaseConfig(pool_pre_ping=True)
        assert config.pool_pre_ping is True


class TestSessionCleanupMiddleware:
    """Tests for SessionCleanupMiddleware."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        session.close = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_session_closed_on_cancelled_error(self, mock_session):
        """Session should be closed when CancelledError is raised."""
        # advanced-alchemy stores session under _aa_connection_state namespace
        scope = {"type": "http", "_aa_connection_state": {"advanced_alchemy_async_session": mock_session}}

        async def cancelling_app(scope, receive, send):
            raise asyncio.CancelledError()

        middleware = SessionCleanupMiddleware(cancelling_app)

        with pytest.raises(asyncio.CancelledError):
            await middleware(scope, AsyncMock(), AsyncMock())

        mock_session.close.assert_called_once()
        assert "advanced_alchemy_async_session" not in scope["_aa_connection_state"]

    @pytest.mark.asyncio
    async def test_normal_operation_not_affected(self, mock_session):
        """Session should NOT be closed during normal operation."""
        scope = {"type": "http", "_aa_connection_state": {"advanced_alchemy_async_session": mock_session}}

        async def normal_app(scope, receive, send):
            pass

        middleware = SessionCleanupMiddleware(normal_app)
        await middleware(scope, AsyncMock(), AsyncMock())

        mock_session.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_http_scope_passthrough(self):
        """Non-HTTP scopes should be passed through without wrapping."""
        inner_app = AsyncMock()
        scope = {"type": "websocket", "state": {}}

        middleware = SessionCleanupMiddleware(inner_app)
        await middleware(scope, AsyncMock(), AsyncMock())

        inner_app.assert_called_once()


class TestPoolPrePingConfiguration:
    """Tests for pool_pre_ping in EngineConfig."""

    def test_engine_config_includes_pool_pre_ping_for_postgresql(self):
        """EngineConfig for PostgreSQL should include pool_pre_ping."""
        from advanced_alchemy.config import EngineConfig

        config = EngineConfig(
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_pre_ping=True,
            echo=False,
        )

        assert config.pool_pre_ping is True

    def test_engine_config_pool_pre_ping_can_be_disabled(self):
        """EngineConfig pool_pre_ping can be disabled."""
        from advanced_alchemy.config import EngineConfig

        config = EngineConfig(
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_pre_ping=False,
            echo=False,
        )

        assert config.pool_pre_ping is False
