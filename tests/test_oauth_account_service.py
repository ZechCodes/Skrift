"""Tests for the shared OAuth account service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from skrift.auth.providers import NormalizedUserData


@pytest.fixture
def user_data():
    return NormalizedUserData(
        oauth_id="oauth-123",
        email="test@example.com",
        name="Test User",
        picture_url="https://photo.url",
    )


@pytest.fixture
def raw_user_info():
    return {"id": "oauth-123", "email": "test@example.com", "name": "Test User"}


@pytest.fixture
def tokens():
    return {"access_token": "access-abc", "refresh_token": "refresh-xyz"}


class TestFindOrCreateOAuthUser:
    @pytest.mark.asyncio
    async def test_existing_oauth_account_updates_and_returns(self, user_data, raw_user_info):
        """When OAuth account exists, update user profile and return."""
        with patch("skrift.auth.oauth_account_service.select"), \
             patch("skrift.auth.oauth_account_service.selectinload"):
            from skrift.auth.oauth_account_service import find_or_create_oauth_user

            mock_user = MagicMock()
            mock_user.id = uuid4()
            mock_user.name = "Old Name"
            mock_user.picture_url = None

            mock_oauth = MagicMock()
            mock_oauth.user = mock_user
            mock_oauth.provider_email = "old@email.com"

            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_oauth
            mock_session.execute.return_value = mock_result

            result = await find_or_create_oauth_user(
                mock_session, "google", user_data, raw_user_info
            )

            assert result.user is mock_user
            assert result.oauth_account is mock_oauth
            assert result.is_new_user is False
            assert mock_user.name == "Test User"
            assert mock_user.picture_url == "https://photo.url"
            assert mock_oauth.provider_email == "test@example.com"

    @pytest.mark.asyncio
    async def test_existing_oauth_account_updates_tokens(self, user_data, raw_user_info, tokens):
        """When OAuth account exists, tokens are updated."""
        with patch("skrift.auth.oauth_account_service.select"), \
             patch("skrift.auth.oauth_account_service.selectinload"):
            from skrift.auth.oauth_account_service import find_or_create_oauth_user

            mock_user = MagicMock()
            mock_user.id = uuid4()
            mock_user.name = "Old Name"
            mock_user.picture_url = None

            mock_oauth = MagicMock()
            mock_oauth.user = mock_user
            mock_oauth.provider_email = "old@email.com"
            mock_oauth.access_token = None
            mock_oauth.refresh_token = None

            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_oauth
            mock_session.execute.return_value = mock_result

            result = await find_or_create_oauth_user(
                mock_session, "google", user_data, raw_user_info, tokens=tokens
            )

            assert result.oauth_account.access_token == "access-abc"
            assert result.oauth_account.refresh_token == "refresh-xyz"

    @pytest.mark.asyncio
    async def test_email_match_links_new_oauth_account(self, user_data, raw_user_info):
        """When no OAuth account but email matches a user, link new account."""
        with patch("skrift.auth.oauth_account_service.select"), \
             patch("skrift.auth.oauth_account_service.selectinload"), \
             patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
            from skrift.auth.oauth_account_service import find_or_create_oauth_user

            mock_user = MagicMock()
            mock_user.id = uuid4()

            mock_session = AsyncMock()
            # First call: OAuth lookup returns None
            # Second call: User email lookup returns user
            mock_result_no_oauth = MagicMock()
            mock_result_no_oauth.scalar_one_or_none.return_value = None
            mock_result_user = MagicMock()
            mock_result_user.scalar_one_or_none.return_value = mock_user
            mock_session.execute.side_effect = [mock_result_no_oauth, mock_result_user]

            result = await find_or_create_oauth_user(
                mock_session, "github", user_data, raw_user_info
            )

            assert result.user is mock_user
            assert result.is_new_user is False
            mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_match_creates_new_user(self, user_data, raw_user_info):
        """When no OAuth account and no email match, create new user + account."""
        with patch("skrift.auth.oauth_account_service.select"), \
             patch("skrift.auth.oauth_account_service.selectinload"), \
             patch("skrift.auth.oauth_account_service.User") as MockUser, \
             patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
            from skrift.auth.oauth_account_service import find_or_create_oauth_user

            mock_new_user = MagicMock()
            mock_new_user.id = uuid4()
            MockUser.return_value = mock_new_user

            mock_session = AsyncMock()
            # First call: OAuth lookup returns None
            # Second call: User email lookup returns None
            mock_result_none = MagicMock()
            mock_result_none.scalar_one_or_none.return_value = None
            mock_session.execute.side_effect = [mock_result_none, mock_result_none]

            result = await find_or_create_oauth_user(
                mock_session, "discord", user_data, raw_user_info
            )

            assert result.is_new_user is True
            # Should have added user and oauth account
            assert mock_session.add.call_count == 2
            mock_session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_user_stores_tokens(self, user_data, raw_user_info, tokens):
        """When creating a new user, tokens are stored on the OAuth account."""
        with patch("skrift.auth.oauth_account_service.select"), \
             patch("skrift.auth.oauth_account_service.selectinload"), \
             patch("skrift.auth.oauth_account_service.User") as MockUser, \
             patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
            from skrift.auth.oauth_account_service import find_or_create_oauth_user

            mock_new_user = MagicMock()
            mock_new_user.id = uuid4()
            MockUser.return_value = mock_new_user

            mock_session = AsyncMock()
            mock_result_none = MagicMock()
            mock_result_none.scalar_one_or_none.return_value = None
            mock_session.execute.side_effect = [mock_result_none, mock_result_none]

            result = await find_or_create_oauth_user(
                mock_session, "discord", user_data, raw_user_info, tokens=tokens
            )

            assert result.is_new_user is True
            MockOAuth.assert_called_once_with(
                provider="discord",
                provider_account_id="oauth-123",
                provider_email="test@example.com",
                provider_metadata=raw_user_info,
                access_token="access-abc",
                refresh_token="refresh-xyz",
                user_id=mock_new_user.id,
            )

    @pytest.mark.asyncio
    async def test_no_email_skips_email_lookup(self, raw_user_info):
        """When email is None, skip the email lookup and create new user."""
        user_data = NormalizedUserData(
            oauth_id="oauth-999", email=None, name="No Email", picture_url=None
        )

        with patch("skrift.auth.oauth_account_service.select"), \
             patch("skrift.auth.oauth_account_service.selectinload"), \
             patch("skrift.auth.oauth_account_service.User") as MockUser, \
             patch("skrift.auth.oauth_account_service.OAuthAccount"):
            from skrift.auth.oauth_account_service import find_or_create_oauth_user

            mock_new_user = MagicMock()
            mock_new_user.id = uuid4()
            MockUser.return_value = mock_new_user

            mock_session = AsyncMock()
            # Only one DB call for OAuth lookup — no email lookup
            mock_result_none = MagicMock()
            mock_result_none.scalar_one_or_none.return_value = None
            mock_session.execute.return_value = mock_result_none

            result = await find_or_create_oauth_user(
                mock_session, "twitter", user_data, raw_user_info
            )

            assert result.is_new_user is True
            # Only one execute call (OAuth lookup, no email lookup)
            assert mock_session.execute.call_count == 1
