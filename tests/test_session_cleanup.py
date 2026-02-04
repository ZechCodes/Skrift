"""Tests for SQLAlchemy session cleanup on request cancellation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.config import DatabaseConfig


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


class TestSafeSQLAlchemyAsyncConfig:
    """Tests for SafeSQLAlchemyAsyncConfig session cleanup."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        session.close = AsyncMock()
        return session

    @pytest.fixture
    def mock_state(self):
        """Create a mock application state."""
        state = MagicMock()
        return state

    @pytest.fixture
    def mock_scope(self):
        """Create a mock ASGI scope."""
        return {"state": {}, "type": "http"}

    @pytest.mark.asyncio
    async def test_session_closed_on_cancelled_error(
        self, mock_session, mock_state, mock_scope
    ):
        """Session should be closed when CancelledError is raised."""
        from skrift.db.session import SafeSQLAlchemyAsyncConfig

        config = SafeSQLAlchemyAsyncConfig(
            connection_string="sqlite+aiosqlite:///./test.db"
        )

        # Mock the session maker to return our mock session
        mock_session_maker = MagicMock(return_value=mock_session)
        mock_state.__getitem__ = MagicMock(return_value=mock_session_maker)

        # Mock the advanced_alchemy utilities
        with (
            patch("skrift.db.session.get_aa_scope_state", return_value=None),
            patch("skrift.db.session.delete_aa_scope_state") as mock_delete,
            patch("skrift.db.session.set_aa_scope_state"),
            patch("skrift.db.session.set_async_context"),
        ):
            # Get the async generator
            gen = config.provide_session(mock_state, mock_scope)

            # Start the generator and get the session
            session = await gen.__anext__()
            assert session is mock_session

            # Simulate a CancelledError
            with pytest.raises(asyncio.CancelledError):
                await gen.athrow(asyncio.CancelledError())

            # Verify session was closed
            mock_session.close.assert_called_once()
            # Verify session was removed from scope state
            mock_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_operation_not_affected(
        self, mock_session, mock_state, mock_scope
    ):
        """Normal session operations should work correctly."""
        from skrift.db.session import SafeSQLAlchemyAsyncConfig

        config = SafeSQLAlchemyAsyncConfig(
            connection_string="sqlite+aiosqlite:///./test.db"
        )

        # Mock the session maker to return our mock session
        mock_session_maker = MagicMock(return_value=mock_session)
        mock_state.__getitem__ = MagicMock(return_value=mock_session_maker)

        # Mock the advanced_alchemy utilities
        with (
            patch("skrift.db.session.get_aa_scope_state", return_value=None),
            patch("skrift.db.session.delete_aa_scope_state") as mock_delete,
            patch("skrift.db.session.set_aa_scope_state"),
            patch("skrift.db.session.set_async_context"),
        ):
            # Get the async generator
            gen = config.provide_session(mock_state, mock_scope)

            # Start the generator and get the session
            session = await gen.__anext__()
            assert session is mock_session

            # Complete normally (no exception)
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass  # Expected - generator is exhausted

            # Session should NOT be closed by the generator in normal operation
            # (that's the job of before_send_handler)
            mock_session.close.assert_not_called()
            mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_reused_from_scope(self, mock_session, mock_state, mock_scope):
        """Existing session in scope should be reused."""
        from skrift.db.session import SafeSQLAlchemyAsyncConfig

        config = SafeSQLAlchemyAsyncConfig(
            connection_string="sqlite+aiosqlite:///./test.db"
        )

        # Mock the advanced_alchemy utilities to return existing session
        with (
            patch("skrift.db.session.get_aa_scope_state", return_value=mock_session),
            patch("skrift.db.session.set_async_context"),
        ):
            # Get the async generator
            gen = config.provide_session(mock_state, mock_scope)

            # Start the generator and get the session
            session = await gen.__anext__()

            # Should return the existing session
            assert session is mock_session


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
