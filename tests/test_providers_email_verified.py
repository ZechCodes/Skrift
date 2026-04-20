"""Per-provider extraction of ``email_verified``.

Regression tests for the account-takeover fix (C1). Every provider must set
``NormalizedUserData.email_verified`` only when it actually attests
verification — any provider that does not expose the signal is treated as
unverified so the email-match branch falls through to the challenge flow.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.auth.providers import (
    DiscordProvider,
    FacebookProvider,
    GenericProvider,
    GitHubProvider,
    GoogleProvider,
    MicrosoftProvider,
    SkriftProvider,
    TwitterProvider,
)
from skrift.setup.providers import get_provider_info


def _info(provider_type: str):
    return get_provider_info(provider_type)


@pytest.mark.parametrize(
    "user_info,expected",
    [
        ({"email": "a@b.com", "verified_email": True}, True),
        ({"email": "a@b.com", "email_verified": True}, True),
        ({"email": "a@b.com", "verified_email": False}, False),
        ({"email": "a@b.com", "email_verified": False}, False),
        ({"email": "a@b.com"}, False),
    ],
)
def test_google_propagates_email_verified(user_info, expected):
    provider = GoogleProvider("google", _info("google"))
    assert provider.extract_user_data(user_info).email_verified is expected


def test_microsoft_always_unverified():
    provider = MicrosoftProvider("microsoft", _info("microsoft"))
    data = provider.extract_user_data({"id": "1", "mail": "a@b.com"})
    assert data.email_verified is False


def test_facebook_always_unverified():
    provider = FacebookProvider("facebook", _info("facebook"))
    data = provider.extract_user_data({"id": "1", "email": "a@b.com", "picture": {"data": {}}})
    assert data.email_verified is False


def test_twitter_always_unverified():
    provider = TwitterProvider("twitter", _info("twitter"))
    data = provider.extract_user_data({"id": "1", "name": "n"})
    assert data.email_verified is False


@pytest.mark.parametrize("verified,expected", [(True, True), (False, False)])
def test_discord_propagates_verified_flag(verified, expected):
    provider = DiscordProvider("discord", _info("discord"))
    data = provider.extract_user_data({"id": "1", "email": "a@b.com", "verified": verified})
    assert data.email_verified is expected


@pytest.mark.parametrize("verified,expected", [(True, True), (False, False)])
def test_skrift_hub_propagates_oidc_flag(verified, expected):
    provider = SkriftProvider("skrift", _info("skrift"))
    data = provider.extract_user_data({"sub": "1", "email": "a@b.com", "email_verified": verified})
    assert data.email_verified is expected


def test_generic_reads_email_verified():
    provider = GenericProvider("custom", _info("skrift"))
    assert provider.extract_user_data({"sub": "1", "email": "a@b.com", "email_verified": True}).email_verified is True
    assert provider.extract_user_data({"sub": "1", "email": "a@b.com"}).email_verified is False


@pytest.mark.asyncio
async def test_github_always_calls_user_emails_and_picks_primary_verified():
    """GitHub's base /user endpoint does not expose a verified flag. The
    provider must always call /user/emails and use the primary+verified entry
    to attest verification."""
    provider = GitHubProvider("github", _info("github"))

    emails_response = MagicMock()
    emails_response.status_code = 200
    emails_response.json.return_value = [
        {"email": "alias@b.com", "primary": False, "verified": True},
        {"email": "primary@b.com", "primary": True, "verified": True},
    ]

    async def _get(url, headers=None):
        return emails_response

    # First call: base /user endpoint
    base_response = MagicMock()
    base_response.status_code = 200
    base_response.json.return_value = {"id": 1, "login": "u", "email": "old@b.com"}

    async def _get_chain(url, headers=None):
        if "emails" in url:
            return emails_response
        return base_response

    client_mock = MagicMock()
    client_mock.get = AsyncMock(side_effect=_get_chain)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=None)

    with patch("skrift.auth.providers.httpx.AsyncClient", return_value=client_mock):
        user_info = await provider.fetch_user_info("token")

    data = provider.extract_user_data(user_info)
    assert data.email == "primary@b.com"
    assert data.email_verified is True


@pytest.mark.asyncio
async def test_github_unverified_primary_produces_unverified_normalized_data():
    provider = GitHubProvider("github", _info("github"))

    emails_response = MagicMock()
    emails_response.status_code = 200
    emails_response.json.return_value = [
        {"email": "primary@b.com", "primary": True, "verified": False},
    ]
    base_response = MagicMock()
    base_response.status_code = 200
    base_response.json.return_value = {"id": 2, "login": "u", "email": "primary@b.com"}

    async def _get_chain(url, headers=None):
        return emails_response if "emails" in url else base_response

    client_mock = MagicMock()
    client_mock.get = AsyncMock(side_effect=_get_chain)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=None)

    with patch("skrift.auth.providers.httpx.AsyncClient", return_value=client_mock):
        user_info = await provider.fetch_user_info("token")

    data = provider.extract_user_data(user_info)
    assert data.email == "primary@b.com"
    assert data.email_verified is False
