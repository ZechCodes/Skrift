"""Tests for session rotation on login."""

from unittest.mock import MagicMock

from skrift.controllers.auth import _set_login_session


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
