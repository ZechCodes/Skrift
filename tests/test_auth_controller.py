"""Tests for the authentication controller."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from skrift.controllers.auth import (
    _is_safe_redirect_url,
    _get_safe_redirect_url,
    _finalize_primary_login,
    _set_login_session,
    _exchange_and_fetch,
)
from skrift.auth.identities import ResolvedPrimaryIdentity
from skrift.auth.session_service import (
    PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
    PendingAuthState,
    PendingAuthTransitionDecision,
)


class TestIsSafeRedirectUrl:
    def test_relative_path_is_safe(self):
        assert _is_safe_redirect_url("/admin", []) is True

    def test_protocol_relative_is_unsafe(self):
        assert _is_safe_redirect_url("//evil.com", []) is False

    def test_https_allowed_domain(self):
        assert _is_safe_redirect_url("https://example.com/page", ["example.com"]) is True

    def test_http_allowed_domain(self):
        assert _is_safe_redirect_url("http://example.com/page", ["example.com"]) is True

    def test_subdomain_matches(self):
        assert _is_safe_redirect_url("https://app.example.com", ["example.com"]) is True

    def test_unknown_domain_rejected(self):
        assert _is_safe_redirect_url("https://evil.com/page", ["example.com"]) is False

    def test_wildcard_pattern(self):
        assert _is_safe_redirect_url("https://app.example.com", ["*.example.com"]) is True

    def test_javascript_scheme_rejected(self):
        assert _is_safe_redirect_url("javascript:alert(1)", ["example.com"]) is False


class TestGetSafeRedirectUrl:
    def test_returns_session_url_if_safe(self):
        request = MagicMock()
        request.session = {"auth_next": "/dashboard"}
        result = _get_safe_redirect_url(request, [])
        assert result == "/dashboard"
        assert "auth_next" not in request.session

    def test_returns_default_if_no_session(self):
        request = MagicMock()
        request.session = {}
        result = _get_safe_redirect_url(request, [])
        assert result == "/"

    def test_returns_default_if_unsafe(self):
        request = MagicMock()
        request.session = {"auth_next": "https://evil.com"}
        result = _get_safe_redirect_url(request, [])
        assert result == "/"


class TestSetLoginSession:
    def test_populates_user_data(self):
        request = MagicMock()
        session_dict = {}
        request.session = session_dict

        user = MagicMock()
        user.id = uuid4()
        user.name = "Test User"
        user.email = "test@example.com"
        user.picture_url = "https://pic.url"

        _set_login_session(request, user)

        assert session_dict["user_id"] == str(user.id)
        assert session_dict["user_name"] == "Test User"
        assert session_dict["user_email"] == "test@example.com"

    def test_preserves_flash(self):
        session_dict = {"flash": "Welcome!", "_nid": "abc123"}
        request = MagicMock()
        request.session = session_dict

        user = MagicMock()
        user.id = uuid4()
        user.name = "User"
        user.email = "u@test.com"
        user.picture_url = None

        _set_login_session(request, user)

        assert session_dict.get("flash") == "Welcome!"
        assert session_dict.get("_nid") == "abc123"


class TestExchangeAndFetch:
    @pytest.mark.asyncio
    async def test_successful_exchange(self):
        """Test successful OAuth code exchange and user info fetch."""
        from skrift.auth.providers import NormalizedUserData

        mock_provider = MagicMock()
        mock_provider.resolve_url.return_value = "https://token.url"
        mock_provider.build_token_data.return_value = {"code": "abc"}
        mock_provider.build_token_headers.return_value = {"Accept": "application/json"}
        mock_provider.provider_info.token_url = "https://token.url"
        mock_provider.extract_user_data.return_value = NormalizedUserData(
            oauth_id="123", email="test@test.com", name="Test", picture_url=None
        )
        mock_provider.fetch_user_info = AsyncMock(return_value={"id": "123"})

        mock_settings = MagicMock()
        mock_settings.auth.providers = {
            "google": MagicMock(client_id="cid", client_secret="secret", tenant_id=None)
        }

        # Mock httpx response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token123"}

        with patch("skrift.auth.oauth_flow.get_oauth_provider", return_value=mock_provider), \
             patch("skrift.auth.oauth_flow.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            user_data, user_info, tokens = await _exchange_and_fetch(
                "google", mock_settings, "code123", "https://redirect"
            )

            assert user_data.oauth_id == "123"
            assert user_info == {"id": "123"}
            assert tokens == {"access_token": "token123"}

    @pytest.mark.asyncio
    async def test_exchange_with_explicit_credentials(self):
        """Test exchange with explicit client_id/secret (setup mode)."""
        from skrift.auth.providers import NormalizedUserData

        mock_provider = MagicMock()
        mock_provider.resolve_url.return_value = "https://token.url"
        mock_provider.build_token_data.return_value = {"code": "abc"}
        mock_provider.build_token_headers.return_value = {}
        mock_provider.provider_info.token_url = "https://token.url"
        mock_provider.extract_user_data.return_value = NormalizedUserData(
            oauth_id="456", email="setup@test.com", name="Admin", picture_url=None
        )
        mock_provider.fetch_user_info = AsyncMock(return_value={"id": "456"})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token456"}

        with patch("skrift.auth.oauth_flow.get_oauth_provider", return_value=mock_provider), \
             patch("skrift.auth.oauth_flow.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            user_data, user_info, tokens = await _exchange_and_fetch(
                "github", None, "code456", "https://redirect",
                client_id="explicit-id", client_secret="explicit-secret",
            )

            assert user_data.oauth_id == "456"
            assert tokens == {"access_token": "token456"}


class TestOAuthLogin:
    @pytest.mark.asyncio
    async def test_unknown_provider_404(self):
        """Unknown provider raises 404."""
        from litestar.exceptions import NotFoundException
        from skrift.controllers.auth import AuthController

        auth = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch("skrift.controllers.auth.get_settings") as mock_settings:
            mock_settings.return_value.auth.providers = {}
            mock_settings.return_value.auth.get_method_keys.return_value = []

            with pytest.raises(NotFoundException):
                await AuthController.oauth_login.fn(auth, request, "nonexistent")


class TestLogout:
    @pytest.mark.asyncio
    async def test_post_with_valid_csrf_clears_session(self):
        from skrift.controllers.auth import AuthController

        auth = AuthController(owner=MagicMock())
        session = {"user_id": "123", "user_name": "test"}
        request = MagicMock()
        request.session = session

        async def _valid_csrf(_req):
            return True

        with patch("skrift.controllers.auth.verify_csrf", new=_valid_csrf):
            await AuthController.logout.fn(auth, request)

        assert len(session) == 0

    @pytest.mark.asyncio
    async def test_post_without_csrf_preserves_session(self):
        from skrift.controllers.auth import AuthController

        auth = AuthController(owner=MagicMock())
        session = {"user_id": "123", "user_name": "test"}
        request = MagicMock()
        request.session = session

        async def _invalid_csrf(_req):
            return False

        with patch("skrift.controllers.auth.verify_csrf", new=_invalid_csrf):
            await AuthController.logout.fn(auth, request)

        # CSRF failure must not end the session — prevents drive-by logout.
        assert session["user_id"] == "123"

    @pytest.mark.asyncio
    async def test_get_renders_confirm_without_clearing_session(self):
        from skrift.controllers.auth import AuthController

        auth = AuthController(owner=MagicMock())
        session = {"user_id": "123", "user_name": "test"}
        request = MagicMock()
        request.session = session

        with patch("skrift.controllers.auth.resolve_template_name", return_value="auth/logout_confirm.html"):
            await AuthController.logout_confirm.fn(auth, request)

        # GET must be safe — no side effects on session.
        assert session["user_id"] == "123"


class TestFinalizePrimaryLogin:
    @pytest.mark.asyncio
    async def test_returns_pending_redirect_when_policy_holds_session(self):
        request = MagicMock()
        request.session = {"auth_next": "/admin"}
        settings = MagicMock()
        settings.auth.allowed_redirect_domains = []
        login_result = MagicMock()
        login_result.user = MagicMock(id="user-1")
        login_result.method_key = "google"
        login_result.method_type = "oauth"
        login_result.is_new_user = False
        identity = ResolvedPrimaryIdentity(
            method_key="google",
            method_type="oauth",
            subject_id="subject-1",
            email="user@example.com",
            name="User",
            picture_url=None,
            raw_metadata={},
            provided_fields={"email", "name"},
        )

        with patch(
            "skrift.controllers.auth.build_second_factor_transition_decision",
            new_callable=AsyncMock,
            return_value=PendingAuthTransitionDecision(
                promote_immediately=False,
                next_url="/auth/verify",
                stage=PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
            ),
        ) as mock_build_decision, patch(
            "skrift.controllers.auth.apply_pending_authentication_transition",
            new_callable=AsyncMock,
            return_value=PendingAuthTransitionDecision(
                promote_immediately=False,
                next_url="/auth/verify",
                stage=PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
            ),
        ) as mock_transition, patch(
            "skrift.controllers.auth.hooks.do_action",
            new_callable=AsyncMock,
        ) as mock_do_action:
            result = await _finalize_primary_login(
                request,
                MagicMock(),
                settings,
                login_result,
                identity=identity,
            )

        assert result.url == "/auth/verify"
        mock_build_decision.assert_awaited_once()
        mock_transition.assert_awaited_once()
        mock_do_action.assert_not_awaited()
        assert request.session["pending_auth_method"] == "google"


class TestVerifyPage:
    @pytest.mark.asyncio
    async def test_redirects_when_pending_auth_is_missing(self):
        from skrift.controllers.auth import AuthController

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch(
            "skrift.controllers.auth.get_pending_authentication",
            return_value=None,
        ), patch(
            "skrift.controllers.auth.flash_error",
        ) as mock_flash:
            result = await AuthController.verify_page.fn(controller, request, MagicMock())

        assert result.url == "/auth/login"
        mock_flash.assert_called_once()

    @pytest.mark.asyncio
    async def test_renders_available_second_factor_methods(self):
        from litestar.response import Template as TemplateResponse
        from skrift.controllers.auth import AuthController
        from skrift.auth.second_factors.base import SecondFactorMethodDescriptor

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.app.template_engine = MagicMock()
        pending_auth = PendingAuthState(
            pending_auth_id="pending-1",
            method_key="google",
            method_type="oauth",
            stage=PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
            user_id="user-1",
            email="user@example.com",
            expires_at=9999999999,
        )
        descriptor = SecondFactorMethodDescriptor(
            key="passkey",
            factor_type="passkey",
            name="Passkey",
            verify_path="/auth/verify/passkey",
        )

        with patch(
            "skrift.controllers.auth.get_pending_authentication",
            return_value=pending_auth,
        ), patch(
            "skrift.controllers.auth.get_settings",
            return_value=MagicMock(),
        ), patch(
            "skrift.controllers.auth.list_available_second_factor_descriptors",
            new_callable=AsyncMock,
            return_value=[descriptor],
        ), patch(
            "skrift.controllers.auth.resolve_template_name",
            return_value="auth/verify.html",
        ):
            result = await AuthController.verify_page.fn(controller, request, MagicMock())

        assert isinstance(result, TemplateResponse)
        assert result.template_name == "auth/verify.html"
        assert result.context["pending_auth"] == pending_auth
        assert result.context["methods"] == [descriptor]


class TestPasskeysPage:
    @pytest.mark.asyncio
    async def test_redirects_anonymous_user_to_login(self):
        from skrift.controllers.auth import AuthController

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch(
            "skrift.controllers.auth._get_authenticated_user",
            new_callable=AsyncMock,
            return_value=None,
        ), patch("skrift.controllers.auth.flash_error") as mock_flash:
            result = await AuthController.passkeys_page.fn(controller, request, MagicMock())

        assert result.url == "/auth/login?next=/auth/passkeys"
        mock_flash.assert_called_once()


class TestPrimaryPasskeyAuth:
    @pytest.mark.asyncio
    async def test_begin_primary_method_options_returns_passkey_options(self):
        from skrift.controllers.auth import AuthController

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {"_csrf_token": "rotated-csrf"}
        settings = MagicMock()
        settings.auth.get_method_keys.return_value = ["passkey"]
        settings.auth.get_primary_auth_method_type.return_value = "passkey"

        with patch(
            "skrift.controllers.auth.get_settings",
            return_value=settings,
        ), patch(
            "skrift.controllers.auth.verify_csrf",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "skrift.controllers.auth.begin_primary_passkey_authentication",
            return_value={"challenge": "challenge-123"},
        ):
            result = await AuthController.begin_primary_method_options.fn(
                controller,
                request,
                "passkey",
            )

        assert result.content["options"]["challenge"] == "challenge-123"
        assert result.content["csrf_token"] == "rotated-csrf"

    @pytest.mark.asyncio
    async def test_begin_primary_method_registration_requires_email(self):
        from skrift.controllers.auth import AuthController

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {"_csrf_token": "rotated-csrf"}
        request.form = AsyncMock(return_value={"email": ""})
        settings = MagicMock()
        settings.auth.get_method_keys.return_value = ["passkey"]
        settings.auth.get_primary_auth_method_type.return_value = "passkey"
        db_session = AsyncMock()

        with patch(
            "skrift.controllers.auth.get_settings",
            return_value=settings,
        ), patch(
            "skrift.controllers.auth.verify_csrf",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await AuthController.begin_primary_method_registration.fn(
                controller,
                request,
                db_session,
                "passkey",
            )

        assert result.content["error"] == "email_required"

    @pytest.mark.asyncio
    async def test_complete_primary_method_registration_returns_redirect(self):
        from skrift.controllers.auth import AuthController

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.form = AsyncMock(return_value={"credential": '{"id":"cred-1"}'})
        db_session = MagicMock()
        db_session.commit = AsyncMock()
        settings = MagicMock()
        settings.auth.get_method_keys.return_value = ["passkey"]
        settings.auth.get_primary_auth_method_type.return_value = "passkey"
        settings.auth.get_method_config.return_value = {"factor_key": "passkey"}
        signup_state = MagicMock(method_key="passkey", email="new@example.com", name="New User")
        registration = MagicMock(credential_id="cred-1")
        login_result = MagicMock()
        login_result.user.email = "new@example.com"
        login_result.user.name = "New User"
        login_result.user.picture_url = None

        with patch(
            "skrift.controllers.auth.get_settings",
            return_value=settings,
        ), patch(
            "skrift.controllers.auth.verify_csrf",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "skrift.controllers.auth.get_primary_passkey_registration_state",
            return_value=signup_state,
        ), patch(
            "skrift.controllers.auth.complete_primary_passkey_registration",
            return_value=registration,
        ), patch(
            "skrift.controllers.auth._create_primary_passkey_signup_login",
            new_callable=AsyncMock,
            return_value=login_result,
        ), patch(
            "skrift.controllers.auth._finalize_primary_login",
            new_callable=AsyncMock,
            return_value=MagicMock(url="/welcome"),
        ) as mock_finalize:
            result = await AuthController.complete_primary_method_registration.fn(
                controller,
                request,
                db_session,
                "passkey",
            )

        assert result.content["redirect"] == "/welcome"
        mock_finalize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_complete_primary_method_auth_returns_redirect(self):
        from skrift.controllers.auth import AuthController

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.form = AsyncMock(return_value={"credential": '{"id":"cred-1"}'})
        db_session = MagicMock()
        db_session.commit = AsyncMock()
        settings = MagicMock()
        settings.auth.get_method_keys.return_value = ["passkey"]
        settings.auth.get_primary_auth_method_type.return_value = "passkey"
        settings.auth.get_method_config.return_value = {"factor_key": "passkey"}
        enrollment = MagicMock()
        login_result = MagicMock()
        login_result.user.email = "user@example.com"
        login_result.user.name = "User"
        login_result.user.picture_url = None

        with patch(
            "skrift.controllers.auth.get_settings",
            return_value=settings,
        ), patch(
            "skrift.controllers.auth.verify_csrf",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "skrift.controllers.auth.get_second_factor_enrollment_by_credential_id",
            new_callable=AsyncMock,
            return_value=enrollment,
        ), patch(
            "skrift.controllers.auth.complete_primary_passkey_authentication",
            return_value=MagicMock(new_sign_count=3, verification_metadata={"user_verified": True}),
        ), patch(
            "skrift.controllers.auth.find_login_result_for_passkey_credential",
            new_callable=AsyncMock,
            return_value=login_result,
        ), patch(
            "skrift.controllers.auth.touch_second_factor_enrollment",
        ) as mock_touch, patch(
            "skrift.controllers.auth._finalize_primary_login",
            new_callable=AsyncMock,
            return_value=MagicMock(url="/admin"),
        ) as mock_finalize:
            result = await AuthController.complete_primary_method_auth.fn(
                controller,
                request,
                db_session,
                "passkey",
            )

        assert result.content["redirect"] == "/admin"
        mock_touch.assert_called_once()
        mock_finalize.assert_awaited_once()


class TestVerifyMethodPage:
    @pytest.mark.asyncio
    async def test_renders_passkey_verification_template(self):
        from litestar.response import Template as TemplateResponse
        from skrift.controllers.auth import AuthController
        from skrift.auth.second_factors.base import SecondFactorMethodDescriptor

        controller = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}
        request.app.template_engine = MagicMock()
        pending_auth = PendingAuthState(
            pending_auth_id="pending-1",
            method_key="google",
            method_type="oauth",
            stage=PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
            user_id="user-1",
            email="user@example.com",
            expires_at=9999999999,
        )
        descriptor = SecondFactorMethodDescriptor(
            key="passkey",
            factor_type="passkey",
            name="Passkey",
            verify_path="/auth/verify/passkey",
        )
        settings = MagicMock()
        settings.auth.second_factors.get_method_type.return_value = "passkey"

        with patch(
            "skrift.controllers.auth.get_pending_authentication",
            return_value=pending_auth,
        ), patch(
            "skrift.controllers.auth.get_settings",
            return_value=settings,
        ), patch(
            "skrift.controllers.auth.list_available_second_factor_descriptors",
            new_callable=AsyncMock,
            return_value=[descriptor],
        ), patch(
            "skrift.controllers.auth.resolve_template_name",
            return_value="auth/verify_passkey.html",
        ):
            result = await AuthController.verify_method_page.fn(
                controller,
                request,
                MagicMock(),
                "passkey",
            )

        assert isinstance(result, TemplateResponse)
        assert result.template_name == "auth/verify_passkey.html"
        assert result.context["factor_key"] == "passkey"
