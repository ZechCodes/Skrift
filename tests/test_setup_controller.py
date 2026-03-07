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
             patch("skrift.setup.controller.can_connect_to_database_url", return_value=(True, None)):
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

        with patch("skrift.setup.controller.update_database_config") as mock_update, \
             patch("skrift.setup.controller.can_connect_to_database_url", return_value=(False, "ECONNREFUSED")):
            result = await SetupController.save_database.fn(controller, request)
            assert result.url == "/setup/database"
            assert "Connection failed" in request.session["setup_error"]
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_env_var_does_not_persist_config(self):
        """Should fail before persisting config when env var is missing."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.form = AsyncMock(return_value={
            "db_type": "postgresql",
            "pg_url_env": "on",
            "pg_url_envvar": "MISSING_DATABASE_URL",
        })

        with patch("skrift.setup.controller.update_database_config") as mock_update:
            result = await SetupController.save_database.fn(controller, request)

        assert result.url == "/setup/database"
        assert request.session["setup_error"] == "Environment variable MISSING_DATABASE_URL is not set"
        mock_update.assert_not_called()


class TestSaveAuth:
    @pytest.mark.asyncio
    async def test_auth_step_prefers_saved_redirect_base_url(self):
        """Should use configured redirect base URL when present."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.headers = {}
        request.url.scheme = "http"
        request.url.netloc = "current.example.com"

        with patch("skrift.setup.controller.load_config", return_value={
            "auth": {
                "redirect_base_url": "https://configured.example.com",
                "providers": {},
            }
        }):
            result = await SetupController.auth_step.fn(controller, request)

        assert result.context["redirect_base_url"] == "https://configured.example.com"

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

    @pytest.mark.asyncio
    async def test_unexpected_error_is_generic_and_logged(self):
        """Unexpected save errors should be logged and not exposed."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.form = AsyncMock(return_value={
            "redirect_base_url": "https://example.com",
            "google_enabled": "on",
            "google_client_id": "id",
            "google_client_secret": "secret",
        })

        provider = MagicMock()
        provider.fields = [
            {"key": "client_id"},
            {"key": "client_secret"},
        ]

        with patch("skrift.setup.controller.get_all_providers", return_value={"google": provider}), \
             patch("skrift.setup.controller.update_auth_config", side_effect=RuntimeError("boom")), \
             patch("skrift.setup.controller.logger.exception") as mock_log:
            result = await SetupController.save_auth.fn(controller, request)

        assert result.url == "/setup/auth"
        assert request.session["setup_error"] == (
            "Could not save authentication settings. Check the server logs and try again."
        )
        mock_log.assert_called_once()


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

    @pytest.mark.asyncio
    async def test_unexpected_error_is_generic_and_logged(self):
        """Unexpected site save errors should be logged and not exposed."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.form = AsyncMock(return_value={
            "site_name": "My Site",
            "site_tagline": "",
            "site_copyright_holder": "",
            "site_copyright_start_year": "",
        })

        with patch("skrift.setup.controller.get_setup_db_session") as mock_session_ctx, \
             patch("skrift.setup.controller.logger.exception") as mock_log:
            mock_session_ctx.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await SetupController.save_site.fn(controller, request)

        assert result.url == "/setup/site"
        assert request.session["setup_error"] == (
            "Could not save site settings. Check the server logs and try again."
        )
        mock_log.assert_called_once()


class TestAdminStep:
    @pytest.mark.asyncio
    async def test_redirects_to_incomplete_step(self):
        """Admin step should redirect until earlier steps are complete."""
        from skrift.setup.controller import SetupController
        from skrift.setup.state import SetupStep

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch("skrift.setup.controller.get_first_incomplete_step", new_callable=AsyncMock, return_value=SetupStep.THEME):
            result = await SetupController.admin_step.fn(controller, request)

        assert result.url == "/setup/theme"

    @pytest.mark.asyncio
    async def test_dummy_login_context_uses_dynamic_step_counts(self):
        """Dummy login should use theme-aware step counts."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch("skrift.setup.controller.load_config", return_value={
            "auth": {"providers": {"dummy": {}}}
        }), \
             patch("skrift.setup.controller._admin_step_number", return_value=5), \
             patch("skrift.setup.controller._total_steps", return_value=5), \
             patch("skrift.setup.controller._get_previous_setup_step_path", return_value="/setup/theme"):
            result = await SetupController.setup_oauth_login.fn(controller, request, "dummy")

        assert result.context["step"] == 5
        assert result.context["total_steps"] == 5
        assert result.context["previous_step_path"] == "/setup/theme"


class TestSetupOAuthCallback:
    @pytest.mark.asyncio
    async def test_setup_oauth_login_uses_configured_redirect_base_url(self):
        """Should build setup redirect URI from saved auth config."""
        from skrift.setup.controller import SetupController

        controller = SetupController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.headers = {}
        request.url.scheme = "http"
        request.url.netloc = "current.example.com"

        provider = MagicMock()
        provider.auth_url = "https://provider.example.com/auth"
        provider.scopes = ["openid"]

        oauth_provider = MagicMock()
        oauth_provider.requires_pkce = False
        oauth_provider.resolve_url.return_value = "https://provider.example.com/auth"
        oauth_provider.build_auth_params.return_value = {
            "client_id": "client-id",
            "redirect_uri": "https://configured.example.com/auth/google/callback",
        }

        with patch("skrift.setup.controller.load_config", return_value={
            "auth": {
                "redirect_base_url": "https://configured.example.com",
                "providers": {"google": {"client_id": "client-id"}},
            }
        }), \
             patch("skrift.setup.controller.get_provider_info", return_value=provider), \
             patch("skrift.auth.providers.get_oauth_provider", return_value=oauth_provider):
            result = await SetupController.setup_oauth_login.fn(controller, request, "google")

        assert "https%3A%2F%2Fconfigured.example.com%2Fauth%2Fgoogle%2Fcallback" in result.url

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

    @pytest.mark.asyncio
    async def test_uses_configured_redirect_base_url_for_callback_exchange(self):
        """Should exchange tokens using the configured callback URL."""
        from skrift.setup.controller import SetupAuthController

        controller = SetupAuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {"oauth_setup": True, "oauth_state": "state"}
        request.headers = {}
        request.url.scheme = "http"
        request.url.netloc = "current.example.com"

        mock_user = MagicMock()
        mock_user.id = "user-id"
        mock_user.name = "User"
        mock_user.email = "user@example.com"
        mock_user.picture_url = None

        with patch("skrift.setup.controller.load_config", return_value={
            "auth": {
                "redirect_base_url": "https://configured.example.com",
                "providers": {"google": {"client_id": "id", "client_secret": "secret"}},
            }
        }), \
             patch("skrift.controllers.auth._exchange_and_fetch", new_callable=AsyncMock) as mock_exchange, \
             patch("skrift.setup.controller.get_setup_db_session") as mock_session_ctx, \
             patch("skrift.auth.oauth_account_service.find_or_create_oauth_user", new_callable=AsyncMock, return_value=MagicMock(user=mock_user)), \
             patch("skrift.setup.controller._finalize_admin_setup", new_callable=AsyncMock):
            mock_exchange.return_value = (MagicMock(oauth_id="id"), {}, {})
            mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

            await SetupAuthController.setup_oauth_callback.fn(
                controller,
                request,
                "google",
                code="abc",
                oauth_state="state",
            )

        assert mock_exchange.await_args.args[3] == "https://configured.example.com/auth/google/callback"

    @pytest.mark.asyncio
    async def test_unexpected_exchange_error_redirects_with_generic_message(self):
        """Unexpected OAuth callback failures should not leak raw errors."""
        from skrift.setup.controller import SetupAuthController

        controller = SetupAuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {"oauth_setup": True, "oauth_state": "state"}

        with patch("skrift.setup.controller.load_config", return_value={
            "auth": {"providers": {"google": {"client_id": "id", "client_secret": "secret"}}}
        }), \
             patch("skrift.controllers.auth._exchange_and_fetch", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("skrift.setup.controller.logger.exception") as mock_log:
            result = await SetupAuthController.setup_oauth_callback.fn(
                controller,
                request,
                "google",
                code="abc",
                oauth_state="state",
            )

        assert result.url == "/setup/admin"
        assert request.session["setup_error"] == (
            "Could not complete authentication. Check the server logs and try again."
        )
        mock_log.assert_called_once()


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
