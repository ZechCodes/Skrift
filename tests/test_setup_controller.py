"""Tests for the setup wizard controller."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSetupIndex:
    @pytest.mark.asyncio
    async def test_redirect_to_welcome(self):
        """Index always redirects to the welcome page."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        result = await SetupController.index.fn(controller, request)
        assert result.url == "/setup/welcome"


class TestDatabaseStep:
    @pytest.mark.asyncio
    async def test_renders_form_when_no_db(self):
        """Should render database form when no DB configured."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch("skrift.setup.controller.can_connect_to_database", return_value=(False, "err")), \
             patch("skrift.setup.controller.load_config", return_value={}):
            result = await SetupController.database_step.fn(controller, request)
            assert result.template_name == "setup/database.html"

    @pytest.mark.asyncio
    async def test_redirects_when_db_connected(self):
        """Should redirect to configuring when DB already configured."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch("skrift.setup.controller.can_connect_to_database", return_value=(True, None)):
            result = await SetupController.database_step.fn(controller, request)
            assert result.url == "/setup/configuring"


class TestSaveDatabase:
    @pytest.mark.asyncio
    async def test_saves_sqlite_config(self):
        """Should save SQLite config and test connection."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        form_mock = AsyncMock(return_value={
            "db_type": "sqlite",
            "sqlite_path": "./test.db",
        })
        request.form = form_mock

        with patch("skrift.setup.controller.update_database_config") as mock_update, \
             patch("skrift.setup.controller.can_connect_to_database", return_value=(True, None)):
            result = await SetupController.save_database.fn(controller, request)
            mock_update.assert_called_once()
            assert result.url == "/setup/configuring"

    @pytest.mark.asyncio
    async def test_connection_failure_redirects_back(self):
        """Should redirect back to database step on connection failure."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        form_mock = AsyncMock(return_value={"db_type": "sqlite", "sqlite_path": "./test.db"})
        request.form = form_mock

        with patch("skrift.setup.controller.update_database_config"), \
             patch("skrift.setup.controller.can_connect_to_database", return_value=(False, "ECONNREFUSED")):
            result = await SetupController.save_database.fn(controller, request)
            assert result.url == "/setup/database"
            assert "Connection failed" in request.session["setup_error"]


class TestSaveAuth:
    @pytest.mark.asyncio
    async def test_no_providers_returns_error(self):
        """Should error if no providers enabled."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        form_mock = AsyncMock(return_value={
            "redirect_base_url": "http://localhost:8000",
        })
        request.form = form_mock

        with patch("skrift.setup.controller.get_all_providers", return_value={"google": MagicMock(fields=[])}):
            result = await SetupController.save_auth.fn(controller, request)
            assert result.url == "/setup/auth"
            assert "at least one" in request.session["setup_error"]


class TestSaveSite:
    @pytest.mark.asyncio
    async def test_requires_site_name(self):
        """Should require site name."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        form_mock = AsyncMock(return_value={
            "site_name": "",
            "site_tagline": "",
            "site_copyright_holder": "",
            "site_copyright_start_year": "",
        })
        request.form = form_mock

        result = await SetupController.save_site.fn(controller, request)
        assert result.url == "/setup/site"
        assert "required" in request.session["setup_error"]


class TestSetupOAuthCallback:
    @pytest.mark.asyncio
    async def test_rejects_non_setup_flow(self):
        """Should reject callbacks not part of setup flow."""
        from litestar.exceptions import HTTPException
        from skrift.setup.controller import SetupAuthController

        controller = SetupAuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with pytest.raises(HTTPException, match="Invalid OAuth flow"):
            await SetupAuthController.setup_oauth_callback.fn(
                controller, request, "google"
            )

    @pytest.mark.asyncio
    async def test_rejects_mismatched_state(self):
        """Should reject mismatched CSRF state."""
        from litestar.exceptions import HTTPException
        from skrift.setup.controller import SetupAuthController

        controller = SetupAuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {"oauth_setup": True, "oauth_state": "correct-state"}

        with pytest.raises(HTTPException, match="Invalid OAuth state"):
            await SetupAuthController.setup_oauth_callback.fn(
                controller, request, "google", code="abc", oauth_state="wrong-state"
            )

    @pytest.mark.asyncio
    async def test_handles_oauth_error(self):
        """Should redirect back to admin step on OAuth error."""
        from skrift.setup.controller import SetupAuthController

        controller = SetupAuthController(owner=MagicMock())
        session = {"oauth_setup": True}
        request = MagicMock()
        request.session = session

        result = await SetupAuthController.setup_oauth_callback.fn(
            controller, request, "google", error="access_denied"
        )
        assert result.url == "/setup/admin"
        assert "access_denied" in session["setup_error"]


class TestResolveEnvVar:
    def test_resolves_env_var(self):
        import os
        from skrift.setup.controller import _resolve_env_var

        os.environ["TEST_SKRIFT_VAR"] = "test_value"
        try:
            assert _resolve_env_var("$TEST_SKRIFT_VAR") == "test_value"
        finally:
            del os.environ["TEST_SKRIFT_VAR"]

    def test_returns_literal_if_no_dollar(self):
        from skrift.setup.controller import _resolve_env_var

        assert _resolve_env_var("literal_value") == "literal_value"

    def test_returns_empty_for_missing_env(self):
        from skrift.setup.controller import _resolve_env_var

        assert _resolve_env_var("$NONEXISTENT_SKRIFT_VAR_12345") == ""
