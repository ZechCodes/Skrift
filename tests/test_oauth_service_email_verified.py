"""Unit tests for ``oauth_service.is_email_verified_for_user`` (issue #151).

The OAuth2 ``/oauth/userinfo`` endpoint derives the ``email_verified`` claim
from this helper. It must report verification from THIS server's own records —
a linked, verified identity for the address — never a blanket true.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skrift.db.services import oauth_service


def _session_returning(row):
    """AsyncMock session whose execute().first() yields ``row``."""
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_returns_true_when_verified_account_matches():
    session = _session_returning((uuid4(),))
    assert await oauth_service.is_email_verified_for_user(
        session, uuid4(), "user@example.com"
    ) is True


@pytest.mark.asyncio
async def test_returns_false_when_no_matching_verified_account():
    session = _session_returning(None)
    assert await oauth_service.is_email_verified_for_user(
        session, uuid4(), "user@example.com"
    ) is False


@pytest.mark.asyncio
async def test_empty_email_short_circuits_without_query():
    session = _session_returning((uuid4(),))
    assert await oauth_service.is_email_verified_for_user(
        session, uuid4(), ""
    ) is False
    session.execute.assert_not_called()
