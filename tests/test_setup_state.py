"""Tests for setup state helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCanConnectToDatabaseUrl:
    @pytest.mark.asyncio
    async def test_returns_success_for_valid_connection(self):
        from skrift.setup.state import can_connect_to_database_url

        mock_conn = AsyncMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.connect.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_engine.dispose = AsyncMock()

        with patch("skrift.setup.state.create_setup_engine", return_value=mock_engine):
            success, error = await can_connect_to_database_url("sqlite+aiosqlite:///./app.db")

        assert success is True
        assert error is None
        mock_conn.execute.assert_awaited_once()
        mock_engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_error_for_failed_connection(self):
        from skrift.setup.state import can_connect_to_database_url

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
        mock_engine.connect.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_engine.dispose = AsyncMock()

        with patch("skrift.setup.state.create_setup_engine", return_value=mock_engine):
            success, error = await can_connect_to_database_url("sqlite+aiosqlite:///./app.db")

        assert success is False
        assert error == "boom"
