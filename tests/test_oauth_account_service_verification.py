"""C1 regression — email-verified gate on OAuth account auto-linking.

When a provider returns an email that matches an existing user but does not
attest that the email is verified, ``find_or_create_user_for_identity`` must
return ``EmailVerificationRequired`` and NOT insert the ``OAuthAccount``
row. This is the fix for the pre-existing-account takeover vector.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from skrift.auth.identities import ResolvedPrimaryIdentity
from skrift.auth.oauth_account_service import (
    EmailVerificationRequired,
    LoginResult,
    complete_verified_email_link,
    find_or_create_user_for_identity,
)


def _identity(*, email: str, verified: bool, subject="sub-1", method="github"):
    return ResolvedPrimaryIdentity(
        method_key=method,
        method_type="oauth",
        subject_id=subject,
        email=email,
        name="U",
        picture_url=None,
        raw_metadata={"id": subject, "email": email},
        provided_fields={"email"},
        email_verified=verified,
    )


def _mock_session(*, oauth=None, user=None, extra_results=None):
    """Build an AsyncMock session with scripted execute() results."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    results = []
    r_oauth = MagicMock()
    r_oauth.scalar_one_or_none.return_value = oauth
    results.append(r_oauth)
    if user is not None or oauth is None:
        r_user = MagicMock()
        r_user.scalar_one_or_none.return_value = user
        results.append(r_user)
    if extra_results:
        results.extend(extra_results)
    session.execute.side_effect = results
    return session


@pytest.mark.asyncio
async def test_unverified_email_match_returns_verification_required_and_skips_insert():
    mock_user = MagicMock()
    mock_user.id = uuid4()
    identity = _identity(email="existing@example.com", verified=False)

    with patch("skrift.auth.oauth_account_service.select"), \
         patch("skrift.auth.oauth_account_service.selectinload"), \
         patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
        session = _mock_session(oauth=None, user=mock_user)
        result = await find_or_create_user_for_identity(session, identity)

    assert isinstance(result, EmailVerificationRequired)
    assert result.existing_user_id == str(mock_user.id)
    # Critical: the row MUST NOT be created until the challenge completes.
    MockOAuth.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_verified_email_match_auto_links_and_sets_provider_email_verified():
    mock_user = MagicMock()
    mock_user.id = uuid4()
    identity = _identity(email="existing@example.com", verified=True)

    with patch("skrift.auth.oauth_account_service.select"), \
         patch("skrift.auth.oauth_account_service.selectinload"), \
         patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
        session = _mock_session(oauth=None, user=mock_user)
        result = await find_or_create_user_for_identity(session, identity)

    assert isinstance(result, LoginResult)
    MockOAuth.assert_called_once()
    kwargs = MockOAuth.call_args.kwargs
    assert kwargs["provider_email_verified"] is True


@pytest.mark.asyncio
async def test_subject_match_bypasses_verification_gate():
    """Subject-ID match is authoritative — verification is not required even
    when the provider does not attest verified, because the OAuth account
    already belongs to this user."""
    mock_user = MagicMock()
    mock_user.id = uuid4()
    mock_oauth = MagicMock()
    mock_oauth.user = mock_user
    mock_oauth.provider_email = "old@example.com"
    identity = _identity(email="new@example.com", verified=False)

    with patch("skrift.auth.oauth_account_service.select"), \
         patch("skrift.auth.oauth_account_service.selectinload"):
        session = _mock_session(oauth=mock_oauth)
        result = await find_or_create_user_for_identity(session, identity)

    assert isinstance(result, LoginResult)
    # Verification attestation is refreshed to match the current identity.
    assert mock_oauth.provider_email_verified is False


@pytest.mark.asyncio
async def test_new_user_records_verified_state():
    identity = _identity(email="brand-new@example.com", verified=True)

    with patch("skrift.auth.oauth_account_service.select"), \
         patch("skrift.auth.oauth_account_service.selectinload"), \
         patch("skrift.auth.oauth_account_service.User") as MockUser, \
         patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
        new_user = MagicMock()
        new_user.id = uuid4()
        MockUser.return_value = new_user
        session = _mock_session(oauth=None, user=None)
        result = await find_or_create_user_for_identity(session, identity)

    assert isinstance(result, LoginResult)
    assert result.is_new_user is True
    assert MockOAuth.call_args.kwargs["provider_email_verified"] is True


@pytest.mark.asyncio
async def test_new_user_records_unverified_state_without_email_challenge():
    """New-user branch has no takeover risk — just record the verified flag
    accurately; no challenge needed because there's no pre-existing account."""
    identity = _identity(email="brand-new@example.com", verified=False)

    with patch("skrift.auth.oauth_account_service.select"), \
         patch("skrift.auth.oauth_account_service.selectinload"), \
         patch("skrift.auth.oauth_account_service.User") as MockUser, \
         patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
        new_user = MagicMock()
        new_user.id = uuid4()
        MockUser.return_value = new_user
        session = _mock_session(oauth=None, user=None)
        result = await find_or_create_user_for_identity(session, identity)

    assert isinstance(result, LoginResult)
    assert MockOAuth.call_args.kwargs["provider_email_verified"] is False


@pytest.mark.asyncio
async def test_complete_verified_email_link_marks_verified_and_creates_row():
    """After the challenge clicker proves email control, the deferred link
    must be created with ``provider_email_verified=True``."""
    mock_user = MagicMock()
    mock_user.id = uuid4()
    identity = _identity(email="existing@example.com", verified=True)

    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = mock_user
    oauth_result = MagicMock()
    oauth_result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.add = MagicMock()
    session.execute.side_effect = [oauth_result, user_result]

    with patch("skrift.auth.oauth_account_service.select"), \
         patch("skrift.auth.oauth_account_service.selectinload"), \
         patch("skrift.auth.oauth_account_service.OAuthAccount") as MockOAuth:
        result = await complete_verified_email_link(
            session,
            existing_user_id=str(mock_user.id),
            identity=identity,
        )

    assert isinstance(result, LoginResult)
    assert MockOAuth.call_args.kwargs["provider_email_verified"] is True
