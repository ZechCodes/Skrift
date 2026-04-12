"""Tests for session rotation on login."""

import pytest
from unittest.mock import MagicMock

from skrift.controllers.auth import _set_login_session
from skrift.auth.identities import ResolvedPrimaryIdentity
from skrift.auth.session_service import (
    PENDING_AUTH_STAGE_PRIMARY_VERIFIED,
    PendingAuthTransitionDecision,
    apply_pending_authentication_transition,
    begin_pending_authentication,
    clear_pending_authentication,
    complete_pending_authentication,
    decide_pending_authentication_transition,
    get_pending_authentication,
    update_pending_authentication,
)


class TestSetLoginSession:
    """Tests for _set_login_session helper."""

    def _make_request(self, session=None):
        request = MagicMock()
        request.session = session if session is not None else {}
        return request

    def _make_user(self, user_id="abc-123", name="Test", email="test@example.com", picture_url=None):
        user = MagicMock()
        user.id = user_id
        user.name = name
        user.email = email
        user.picture_url = picture_url
        return user

    def test_session_is_cleared(self):
        """Session should be cleared (rotated) during login."""
        session = {"old_key": "old_value", "oauth_state": "stale"}
        request = self._make_request(session=session)
        user = self._make_user()

        _set_login_session(request, user)

        assert "old_key" not in request.session
        assert "oauth_state" not in request.session

    def test_user_data_is_set(self):
        """User data should be populated in session after login."""
        request = self._make_request()
        user = self._make_user(
            user_id="user-1", name="Alice", email="alice@test.com", picture_url="https://img.example.com/alice.jpg"
        )

        _set_login_session(request, user)

        assert request.session["user_id"] == "user-1"
        assert request.session["user_name"] == "Alice"
        assert request.session["user_email"] == "alice@test.com"
        assert request.session["user_picture_url"] == "https://img.example.com/alice.jpg"

    def test_flash_is_preserved(self):
        """Flash messages set before rotation should survive."""
        session = {"flash": "Successfully logged in!"}
        request = self._make_request(session=session)
        user = self._make_user()

        _set_login_session(request, user)

        assert request.session["flash"] == "Successfully logged in!"

    def test_flash_messages_list_is_preserved(self):
        """flash_messages list should also survive rotation."""
        messages = [{"type": "success", "text": "Welcome!"}]
        session = {"flash_messages": messages}
        request = self._make_request(session=session)
        user = self._make_user()

        _set_login_session(request, user)

        assert request.session["flash_messages"] == messages

    def test_no_flash_when_absent(self):
        """When no flash state exists, it's not injected."""
        request = self._make_request()
        user = self._make_user()

        _set_login_session(request, user)

        assert "flash" not in request.session
        assert "flash_messages" not in request.session


class TestPendingAuthSession:
    """Tests for pending-auth session helpers."""

    def _make_request(self, session=None):
        request = MagicMock()
        request.session = session if session is not None else {}
        return request

    def _make_user(self, user_id="abc-123", name="Test", email="test@example.com", picture_url=None):
        user = MagicMock()
        user.id = user_id
        user.name = name
        user.email = email
        user.picture_url = picture_url
        return user

    def _make_identity(self):
        return ResolvedPrimaryIdentity(
            method_key="google",
            method_type="oauth",
            subject_id="oauth-subject-1",
            email="test@example.com",
            name="Test User",
            picture_url=None,
            raw_metadata={"sub": "oauth-subject-1"},
            provided_fields={"email", "name"},
        )

    def test_begin_pending_authentication_populates_session(self):
        request = self._make_request()

        pending_auth = begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )

        assert request.session["pending_auth_id"] == pending_auth.pending_auth_id
        assert request.session["pending_auth_method"] == "google"
        assert request.session["pending_auth_method_type"] == "oauth"
        assert request.session["pending_auth_stage"] == PENDING_AUTH_STAGE_PRIMARY_VERIFIED
        assert request.session["pending_auth_user_id"] == "user-1"

    def test_get_pending_authentication_returns_state(self):
        request = self._make_request()
        created = begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )

        loaded = get_pending_authentication(request)

        assert loaded is not None
        assert loaded.pending_auth_id == created.pending_auth_id
        assert loaded.method_key == "google"
        assert loaded.subject_id == "oauth-subject-1"

    def test_get_pending_authentication_clears_expired_state(self):
        request = self._make_request(
            session={
                "pending_auth_id": "expired",
                "pending_auth_method": "google",
                "pending_auth_method_type": "oauth",
                "pending_auth_stage": PENDING_AUTH_STAGE_PRIMARY_VERIFIED,
                "pending_auth_expires_at": 1,
            }
        )

        loaded = get_pending_authentication(request)

        assert loaded is None
        assert "pending_auth_id" not in request.session

    def test_clear_pending_authentication_removes_pending_keys(self):
        request = self._make_request()
        begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )

        clear_pending_authentication(request)

        assert "pending_auth_id" not in request.session
        assert "pending_auth_method" not in request.session

    def test_complete_pending_authentication_promotes_user_session(self):
        request = self._make_request(session={"flash": "Welcome back"})
        user = self._make_user(user_id="user-1", name="Alice", email="alice@test.com")
        pending_auth = begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )

        complete_pending_authentication(request, user, pending_auth=pending_auth)

        assert request.session["user_id"] == "user-1"
        assert request.session["user_email"] == "alice@test.com"
        assert "pending_auth_id" not in request.session
        assert request.session["flash"] == "Welcome back"

    def test_complete_pending_authentication_requires_pending_state(self):
        request = self._make_request()
        user = self._make_user()

        try:
            complete_pending_authentication(request, user)
        except ValueError as exc:
            assert str(exc) == "No pending authentication session found"
        else:
            raise AssertionError("Expected ValueError when pending auth is absent")

    def test_update_pending_authentication_changes_stage(self):
        request = self._make_request()
        begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )

        updated = update_pending_authentication(request, stage="second_factor_required")

        assert updated.stage == "second_factor_required"
        assert request.session["pending_auth_stage"] == "second_factor_required"

    @pytest.mark.asyncio
    async def test_decide_pending_authentication_transition_defaults_to_immediate_promotion(self):
        request = self._make_request()
        pending_auth = begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )
        login_result = MagicMock()

        decision = await decide_pending_authentication_transition(request, login_result, pending_auth)

        assert decision.promote_immediately is True
        assert decision.next_url is None

    @pytest.mark.asyncio
    async def test_apply_pending_authentication_transition_can_leave_session_pending(self):
        request = self._make_request()
        user = self._make_user()
        pending_auth = begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )
        login_result = MagicMock()

        async def hold_for_second_factor(decision, *_args):
            return PendingAuthTransitionDecision(
                promote_immediately=False,
                next_url="/auth/verify",
                stage="second_factor_required",
            )

        from skrift.lib.hooks import AUTH_PENDING_AUTHENTICATION, hooks

        hooks.add_filter(AUTH_PENDING_AUTHENTICATION, hold_for_second_factor)
        try:
            decision = await apply_pending_authentication_transition(
                request,
                user,
                login_result=login_result,
                pending_auth=pending_auth,
            )
        finally:
            hooks.clear()

        assert decision.promote_immediately is False
        assert decision.next_url == "/auth/verify"
        assert request.session["pending_auth_stage"] == "second_factor_required"
        assert "user_id" not in request.session
        assert request.session["pending_auth_id"] == pending_auth.pending_auth_id

    @pytest.mark.asyncio
    async def test_apply_pending_authentication_transition_requires_next_url_when_held(self):
        request = self._make_request()
        user = self._make_user()
        pending_auth = begin_pending_authentication(
            request,
            method_key="google",
            method_type="oauth",
            identity=self._make_identity(),
            user_id="user-1",
        )
        login_result = MagicMock()

        async def hold_without_redirect(decision, *_args):
            return PendingAuthTransitionDecision(promote_immediately=False)

        from skrift.lib.hooks import AUTH_PENDING_AUTHENTICATION, hooks

        hooks.add_filter(AUTH_PENDING_AUTHENTICATION, hold_without_redirect)
        try:
            with pytest.raises(ValueError, match="must provide next_url"):
                await apply_pending_authentication_transition(
                    request,
                    user,
                    login_result=login_result,
                    pending_auth=pending_auth,
                )
        finally:
            hooks.clear()
