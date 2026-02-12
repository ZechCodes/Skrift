"""Tests for OAuth-related functions in skrift.controllers.auth."""

import uuid
from dataclasses import fields
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.controllers.auth import (
    UserData,
    _is_safe_redirect_url,
    _set_login_session,
    extract_user_data,
    find_or_create_user_for_oauth,
)


# ---------------------------------------------------------------------------
# 1. Provider extraction registry
# ---------------------------------------------------------------------------


class TestExtractUserDataGoogle:
    def test_returns_correct_fields(self):
        info = {
            "id": "google-123",
            "email": "alice@gmail.com",
            "name": "Alice",
            "picture": "https://lh3.googleusercontent.com/photo.jpg",
        }
        result = extract_user_data("google", info)
        assert result.oauth_id == "google-123"
        assert result.email == "alice@gmail.com"
        assert result.name == "Alice"
        assert result.picture_url == "https://lh3.googleusercontent.com/photo.jpg"

    def test_missing_fields_return_none(self):
        result = extract_user_data("google", {})
        assert result.oauth_id is None
        assert result.email is None
        assert result.name is None
        assert result.picture_url is None


class TestExtractUserDataGitHub:
    def test_converts_id_to_string(self):
        info = {
            "id": 12345,
            "email": "dev@github.com",
            "name": "Dev User",
            "avatar_url": "https://avatars.githubusercontent.com/u/12345",
        }
        result = extract_user_data("github", info)
        assert result.oauth_id == "12345"
        assert isinstance(result.oauth_id, str)

    def test_falls_back_to_login_for_name(self):
        info = {"id": 99, "login": "octocat"}
        result = extract_user_data("github", info)
        assert result.name == "octocat"

    def test_prefers_name_over_login(self):
        info = {"id": 99, "name": "Octocat User", "login": "octocat"}
        result = extract_user_data("github", info)
        assert result.name == "Octocat User"

    def test_avatar_url(self):
        info = {"id": 1, "avatar_url": "https://avatars.githubusercontent.com/u/1"}
        result = extract_user_data("github", info)
        assert result.picture_url == "https://avatars.githubusercontent.com/u/1"


class TestExtractUserDataMicrosoft:
    def test_uses_mail_for_email(self):
        info = {
            "id": "ms-abc",
            "mail": "user@outlook.com",
            "userPrincipalName": "user@corp.onmicrosoft.com",
            "displayName": "MS User",
        }
        result = extract_user_data("microsoft", info)
        assert result.email == "user@outlook.com"

    def test_falls_back_to_userPrincipalName(self):
        info = {
            "id": "ms-abc",
            "mail": None,
            "userPrincipalName": "user@corp.onmicrosoft.com",
            "displayName": "MS User",
        }
        result = extract_user_data("microsoft", info)
        assert result.email == "user@corp.onmicrosoft.com"

    def test_falls_back_to_userPrincipalName_when_mail_missing(self):
        info = {
            "id": "ms-abc",
            "userPrincipalName": "user@corp.onmicrosoft.com",
            "displayName": "MS User",
        }
        result = extract_user_data("microsoft", info)
        assert result.email == "user@corp.onmicrosoft.com"

    def test_picture_url_always_none(self):
        info = {"id": "ms-abc", "displayName": "User"}
        result = extract_user_data("microsoft", info)
        assert result.picture_url is None


class TestExtractUserDataDiscord:
    def test_constructs_avatar_url(self):
        info = {
            "id": "111222333",
            "email": "discord@example.com",
            "global_name": "CoolUser",
            "avatar": "abc123hash",
        }
        result = extract_user_data("discord", info)
        assert result.picture_url == "https://cdn.discordapp.com/avatars/111222333/abc123hash.png"

    def test_avatar_url_none_when_no_avatar(self):
        info = {"id": "111222333", "email": "d@example.com", "username": "user"}
        result = extract_user_data("discord", info)
        assert result.picture_url is None

    def test_avatar_url_none_when_no_user_id(self):
        info = {"avatar": "abc123hash", "username": "user"}
        result = extract_user_data("discord", info)
        assert result.picture_url is None

    def test_falls_back_to_username_for_name(self):
        info = {"id": "111", "username": "fallback_user"}
        result = extract_user_data("discord", info)
        assert result.name == "fallback_user"

    def test_prefers_global_name_over_username(self):
        info = {"id": "111", "global_name": "Display Name", "username": "user"}
        result = extract_user_data("discord", info)
        assert result.name == "Display Name"


class TestExtractUserDataFacebook:
    def test_extracts_picture_url_from_nested_data(self):
        info = {
            "id": "fb-999",
            "email": "fb@example.com",
            "name": "FB User",
            "picture": {
                "data": {
                    "url": "https://graph.facebook.com/999/picture",
                    "is_silhouette": False,
                }
            },
        }
        result = extract_user_data("facebook", info)
        assert result.picture_url == "https://graph.facebook.com/999/picture"

    def test_silhouette_picture_returns_none(self):
        info = {
            "id": "fb-999",
            "name": "FB User",
            "picture": {
                "data": {
                    "url": "https://graph.facebook.com/default.jpg",
                    "is_silhouette": True,
                }
            },
        }
        result = extract_user_data("facebook", info)
        assert result.picture_url is None

    def test_missing_picture_key(self):
        info = {"id": "fb-999", "name": "FB User"}
        result = extract_user_data("facebook", info)
        assert result.picture_url is None

    def test_empty_picture_data(self):
        info = {"id": "fb-999", "picture": {}}
        result = extract_user_data("facebook", info)
        assert result.picture_url is None


class TestExtractUserDataTwitter:
    def test_returns_none_picture(self):
        info = {
            "id": "tw-42",
            "email": "tw@example.com",
            "name": "Twitter User",
            "username": "tw_user",
        }
        result = extract_user_data("twitter", info)
        assert result.picture_url is None

    def test_prefers_name_over_username(self):
        info = {"id": "tw-42", "name": "Real Name", "username": "handle"}
        result = extract_user_data("twitter", info)
        assert result.name == "Real Name"

    def test_falls_back_to_username(self):
        info = {"id": "tw-42", "username": "handle"}
        result = extract_user_data("twitter", info)
        assert result.name == "handle"


class TestExtractUserDataUnknown:
    def test_uses_default_extractor(self):
        info = {
            "id": "unknown-1",
            "email": "unknown@example.com",
            "name": "Unknown Provider User",
            "picture": "https://example.com/pic.jpg",
        }
        result = extract_user_data("some_unknown_provider", info)
        assert result.oauth_id == "unknown-1"
        assert result.email == "unknown@example.com"
        assert result.name == "Unknown Provider User"
        assert result.picture_url == "https://example.com/pic.jpg"

    def test_default_falls_back_to_sub_for_id(self):
        info = {"sub": "sub-value-123", "email": "u@example.com"}
        result = extract_user_data("brand_new_provider", info)
        assert result.oauth_id == "sub-value-123"

    def test_default_prefers_id_over_sub(self):
        info = {"id": "id-value", "sub": "sub-value"}
        result = extract_user_data("brand_new_provider", info)
        assert result.oauth_id == "id-value"


class TestUserDataIsDataclass:
    def test_returned_object_is_userdata_dataclass(self):
        result = extract_user_data("google", {"id": "1"})
        assert isinstance(result, UserData)

    def test_userdata_has_expected_fields(self):
        field_names = {f.name for f in fields(UserData)}
        assert field_names == {"oauth_id", "email", "name", "picture_url"}


# ---------------------------------------------------------------------------
# 2. find_or_create_user_for_oauth()
# ---------------------------------------------------------------------------


def _make_mock_user(
    user_id=None,
    email="test@example.com",
    name="Test User",
    picture_url=None,
):
    """Create a MagicMock that behaves like a User ORM instance."""
    user = MagicMock()
    user.id = user_id or uuid.uuid4()
    user.email = email
    user.name = name
    user.picture_url = picture_url
    user.last_login_at = None
    return user


def _make_mock_oauth_account(provider, provider_account_id, user):
    """Create a MagicMock that behaves like an OAuthAccount ORM instance."""
    acct = MagicMock()
    acct.provider = provider
    acct.provider_account_id = provider_account_id
    acct.user = user
    acct.provider_email = None
    acct.provider_metadata = None
    return acct


def _make_mock_db_session():
    """Create a mock AsyncSession with execute/commit/flush/add."""
    db = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
class TestFindOrCreateUserForOAuth:
    async def test_existing_oauth_account_updates_and_returns_user(self):
        """Step 1: OAuthAccount already exists -> update user and return it."""
        existing_user = _make_mock_user(name="Old Name", picture_url="old.jpg")
        existing_oauth = _make_mock_oauth_account("google", "goog-1", existing_user)

        db_session = _make_mock_db_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_oauth
        db_session.execute.return_value = mock_result

        user = await find_or_create_user_for_oauth(
            db_session,
            provider="google",
            oauth_id="goog-1",
            email="new@example.com",
            name="New Name",
            picture_url="https://new-pic.jpg",
            provider_metadata={"raw": "data"},
        )

        assert user is existing_user
        assert user.name == "New Name"
        assert user.picture_url == "https://new-pic.jpg"
        assert existing_oauth.provider_email == "new@example.com"
        assert existing_oauth.provider_metadata == {"raw": "data"}
        db_session.commit.assert_awaited_once()
        # Should NOT call db_session.add since both objects already exist
        db_session.add.assert_not_called()

    async def test_existing_oauth_account_does_not_overwrite_picture_with_none(self):
        """When picture_url is None, existing picture should be preserved."""
        existing_user = _make_mock_user(picture_url="existing.jpg")
        existing_oauth = _make_mock_oauth_account("google", "goog-1", existing_user)

        db_session = _make_mock_db_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_oauth
        db_session.execute.return_value = mock_result

        user = await find_or_create_user_for_oauth(
            db_session,
            provider="google",
            oauth_id="goog-1",
            email="test@example.com",
            name="Name",
            picture_url=None,
            provider_metadata={},
        )

        assert user.picture_url == "existing.jpg"

    async def test_no_oauth_but_user_with_email_found_links_account(self):
        """Step 2: No OAuthAccount, but user with matching email exists -> link."""
        existing_user = _make_mock_user(email="match@example.com")

        db_session = _make_mock_db_session()

        # First execute: OAuthAccount lookup -> None
        oauth_result = MagicMock()
        oauth_result.scalar_one_or_none.return_value = None
        # Second execute: User lookup by email -> existing_user
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = existing_user

        db_session.execute.side_effect = [oauth_result, user_result]

        mock_select = MagicMock()
        mock_select.return_value = mock_select
        mock_select.options.return_value = mock_select
        mock_select.where.return_value = mock_select

        with patch("skrift.controllers.auth.select", mock_select), \
             patch("skrift.controllers.auth.selectinload", MagicMock()), \
             patch("skrift.controllers.auth.OAuthAccount") as MockOAuth:
            mock_oauth_instance = MagicMock()
            MockOAuth.return_value = mock_oauth_instance

            user = await find_or_create_user_for_oauth(
                db_session,
                provider="github",
                oauth_id="gh-99",
                email="match@example.com",
                name="Linked User",
                picture_url="https://linked.jpg",
                provider_metadata={"gh": True},
            )

        assert user is existing_user
        assert user.name == "Linked User"
        assert user.picture_url == "https://linked.jpg"
        db_session.add.assert_called_once_with(mock_oauth_instance)
        MockOAuth.assert_called_once_with(
            provider="github",
            provider_account_id="gh-99",
            provider_email="match@example.com",
            provider_metadata={"gh": True},
            user_id=existing_user.id,
        )
        db_session.commit.assert_awaited_once()

    async def test_no_oauth_no_user_creates_both(self):
        """Step 3: No OAuthAccount, no existing user -> create both."""
        db_session = _make_mock_db_session()

        # First execute: OAuthAccount lookup -> None
        oauth_result = MagicMock()
        oauth_result.scalar_one_or_none.return_value = None
        # Second execute: User lookup by email -> None
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None

        db_session.execute.side_effect = [oauth_result, user_result]

        mock_user_instance = MagicMock()
        mock_user_instance.id = uuid.uuid4()
        mock_oauth_instance = MagicMock()

        mock_select = MagicMock()
        mock_select.return_value = mock_select
        mock_select.options.return_value = mock_select
        mock_select.where.return_value = mock_select

        with patch("skrift.controllers.auth.select", mock_select), \
             patch("skrift.controllers.auth.selectinload", MagicMock()), \
             patch("skrift.controllers.auth.User", return_value=mock_user_instance) as MockUser, \
             patch("skrift.controllers.auth.OAuthAccount", return_value=mock_oauth_instance) as MockOAuth:

            user = await find_or_create_user_for_oauth(
                db_session,
                provider="discord",
                oauth_id="disc-77",
                email="brand-new@example.com",
                name="Brand New",
                picture_url="https://brand-new.jpg",
                provider_metadata={"discord": True},
            )

        assert user is mock_user_instance
        MockUser.assert_called_once()
        call_kwargs = MockUser.call_args[1]
        assert call_kwargs["email"] == "brand-new@example.com"
        assert call_kwargs["name"] == "Brand New"
        assert call_kwargs["picture_url"] == "https://brand-new.jpg"

        # Both user and oauth account should have been added
        assert db_session.add.call_count == 2
        db_session.flush.assert_awaited_once()
        db_session.commit.assert_awaited_once()

        MockOAuth.assert_called_once_with(
            provider="discord",
            provider_account_id="disc-77",
            provider_email="brand-new@example.com",
            provider_metadata={"discord": True},
            user_id=mock_user_instance.id,
        )

    async def test_none_email_creates_new_user_without_email_lookup(self):
        """When email is None, skip the email-based user lookup and create new."""
        db_session = _make_mock_db_session()

        # Only one execute call: OAuthAccount lookup -> None
        # No second call because email is None, so email-lookup is skipped
        oauth_result = MagicMock()
        oauth_result.scalar_one_or_none.return_value = None
        db_session.execute.side_effect = [oauth_result]

        mock_user_instance = MagicMock()
        mock_user_instance.id = uuid.uuid4()
        mock_oauth_instance = MagicMock()

        mock_select = MagicMock()
        mock_select.return_value = mock_select
        mock_select.options.return_value = mock_select
        mock_select.where.return_value = mock_select

        with patch("skrift.controllers.auth.select", mock_select), \
             patch("skrift.controllers.auth.selectinload", MagicMock()), \
             patch("skrift.controllers.auth.User", return_value=mock_user_instance), \
             patch("skrift.controllers.auth.OAuthAccount", return_value=mock_oauth_instance):

            user = await find_or_create_user_for_oauth(
                db_session,
                provider="twitter",
                oauth_id="tw-1",
                email=None,
                name="No Email User",
                picture_url=None,
                provider_metadata={},
            )

        assert user is mock_user_instance
        # execute called only once (oauth lookup), no email lookup
        db_session.execute.assert_awaited_once()
        db_session.add.assert_called()
        db_session.flush.assert_awaited_once()
        db_session.commit.assert_awaited_once()

    async def test_existing_oauth_account_does_not_overwrite_email_with_none(self):
        """When email is None on existing OAuth account, provider_email is not updated."""
        existing_user = _make_mock_user()
        existing_oauth = _make_mock_oauth_account("google", "goog-1", existing_user)
        existing_oauth.provider_email = "old@example.com"

        db_session = _make_mock_db_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_oauth
        db_session.execute.return_value = mock_result

        await find_or_create_user_for_oauth(
            db_session,
            provider="google",
            oauth_id="goog-1",
            email=None,
            name="Name",
            picture_url=None,
            provider_metadata={},
        )

        # provider_email should not have been overwritten
        assert existing_oauth.provider_email == "old@example.com"


# ---------------------------------------------------------------------------
# 3. _is_safe_redirect_url()
# ---------------------------------------------------------------------------


class TestIsSafeRedirectUrl:
    def test_relative_url_is_safe(self):
        assert _is_safe_redirect_url("/dashboard", []) is True

    def test_relative_url_with_query_is_safe(self):
        assert _is_safe_redirect_url("/page?id=1", []) is True

    def test_protocol_relative_url_is_not_safe(self):
        assert _is_safe_redirect_url("//evil.com", []) is False

    def test_protocol_relative_with_path_is_not_safe(self):
        assert _is_safe_redirect_url("//evil.com/steal", []) is False

    def test_matching_domain(self):
        assert _is_safe_redirect_url(
            "https://app.example.com/path", ["app.example.com"]
        ) is True

    def test_subdomain_matches_bare_domain(self):
        assert _is_safe_redirect_url(
            "https://sub.example.com/path", ["example.com"]
        ) is True

    def test_wildcard_subdomain_matching(self):
        assert _is_safe_redirect_url(
            "https://foo.example.com/path", ["*.example.com"]
        ) is True

    def test_wildcard_does_not_match_bare_domain(self):
        assert _is_safe_redirect_url(
            "https://example.com/path", ["*.example.com"]
        ) is False

    def test_non_matching_domain(self):
        assert _is_safe_redirect_url(
            "https://evil.com/path", ["example.com"]
        ) is False

    def test_non_http_scheme_rejected(self):
        assert _is_safe_redirect_url(
            "javascript:alert(1)", ["example.com"]
        ) is False

    def test_ftp_scheme_rejected(self):
        assert _is_safe_redirect_url(
            "ftp://example.com/file", ["example.com"]
        ) is False

    def test_http_scheme_accepted(self):
        assert _is_safe_redirect_url(
            "http://example.com/path", ["example.com"]
        ) is True

    def test_empty_url_not_safe(self):
        assert _is_safe_redirect_url("", []) is False

    def test_domain_with_port_matches(self):
        assert _is_safe_redirect_url(
            "https://example.com:8443/path", ["example.com"]
        ) is True

    def test_case_insensitive_matching(self):
        assert _is_safe_redirect_url(
            "https://Example.COM/path", ["example.com"]
        ) is True

    def test_prefix_pattern_wildcard(self):
        assert _is_safe_redirect_url(
            "https://app-staging.example.com/path", ["app-*.example.com"]
        ) is True

    def test_prefix_pattern_wildcard_no_match(self):
        assert _is_safe_redirect_url(
            "https://other.example.com/path", ["app-*.example.com"]
        ) is False


# ---------------------------------------------------------------------------
# 4. _set_login_session()
# ---------------------------------------------------------------------------


class TestSetLoginSession:
    def _make_request_with_session(self, session_data=None):
        """Create a mock request whose session behaves like a real dict."""
        request = MagicMock()
        session = dict(session_data or {})

        # Make request.session act like a real dict
        request.session = session
        return request

    def _make_user(self, user_id=None, name="Test", email="t@e.com", picture_url=None):
        user = MagicMock()
        user.id = user_id or uuid.uuid4()
        user.name = name
        user.email = email
        user.picture_url = picture_url
        return user

    def test_sets_user_data_in_session(self):
        user = self._make_user(name="Alice", email="alice@example.com", picture_url="pic.jpg")
        request = self._make_request_with_session()

        _set_login_session(request, user)

        assert request.session["user_id"] == str(user.id)
        assert request.session["user_name"] == "Alice"
        assert request.session["user_email"] == "alice@example.com"
        assert request.session["user_picture_url"] == "pic.jpg"

    def test_clears_old_session_data(self):
        request = self._make_request_with_session({"stale_key": "should_be_gone"})
        user = self._make_user()

        _set_login_session(request, user)

        assert "stale_key" not in request.session

    def test_preserves_flash_messages_across_rotation(self):
        request = self._make_request_with_session({
            "flash": "Successfully logged in!",
            "flash_messages": [{"type": "success", "text": "Welcome!"}],
            "other_data": "should_be_removed",
        })
        user = self._make_user()

        _set_login_session(request, user)

        assert request.session["flash"] == "Successfully logged in!"
        assert request.session["flash_messages"] == [{"type": "success", "text": "Welcome!"}]
        assert "other_data" not in request.session

    def test_does_not_set_flash_keys_when_absent(self):
        request = self._make_request_with_session()
        user = self._make_user()

        _set_login_session(request, user)

        assert "flash" not in request.session
        assert "flash_messages" not in request.session

    def test_preserves_flash_but_not_flash_messages_when_only_flash_present(self):
        request = self._make_request_with_session({"flash": "Hello"})
        user = self._make_user()

        _set_login_session(request, user)

        assert request.session["flash"] == "Hello"
        assert "flash_messages" not in request.session

    def test_handles_none_user_fields(self):
        user = self._make_user(name=None, email=None, picture_url=None)
        request = self._make_request_with_session()

        _set_login_session(request, user)

        assert request.session["user_name"] is None
        assert request.session["user_email"] is None
        assert request.session["user_picture_url"] is None
