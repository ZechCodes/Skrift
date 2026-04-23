"""M6 — `/oauth/userinfo` honors granted scope strictly.

Previously an access token with an empty `scope` field triggered a
backwards-compat branch that returned the full profile + email claim
set — a silent scope bypass.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.auth.tokens import create_signed_token
from skrift.controllers.oauth2 import ACCESS_TOKEN_TTL, OAuth2Controller


SECRET = "test-secret-key"


def _settings():
    s = MagicMock()
    s.secret_key = SECRET
    return s


def _access_token(scope: str) -> str:
    return create_signed_token(
        {
            "type": "access",
            "user_id": "user-42",
            "email": "u@example.com",
            "name": "U User",
            "picture_url": "https://x/p.png",
            "client_id": "abc",
            "scope": scope,
        },
        SECRET,
        ACCESS_TOKEN_TTL,
    )


async def _userinfo(token: str) -> MagicMock:
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.headers = {"authorization": f"Bearer {token}"}
    db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
        mock_svc.is_token_revoked = AsyncMock(return_value=False)
        return await OAuth2Controller.userinfo.fn(controller, request, db_session)


@pytest.mark.asyncio
async def test_empty_scope_returns_only_sub():
    """A token minted with empty scope must NOT leak email/name/picture.
    This is the M6 regression — the prior behavior returned everything."""
    result = await _userinfo(_access_token(""))
    assert result.status_code == 200
    assert result.content == {"sub": "user-42"}
    assert "email" not in result.content
    assert "name" not in result.content
    assert "picture" not in result.content


@pytest.mark.asyncio
async def test_openid_scope_returns_only_sub():
    result = await _userinfo(_access_token("openid"))
    assert result.content == {"sub": "user-42"}


@pytest.mark.asyncio
async def test_email_scope_returns_sub_and_email():
    result = await _userinfo(_access_token("openid email"))
    assert result.content == {"sub": "user-42", "email": "u@example.com"}


@pytest.mark.asyncio
async def test_profile_scope_returns_sub_name_picture():
    result = await _userinfo(_access_token("openid profile"))
    assert result.content == {
        "sub": "user-42",
        "name": "U User",
        "picture": "https://x/p.png",
    }


@pytest.mark.asyncio
async def test_all_scopes_returns_full_claim_set():
    result = await _userinfo(_access_token("openid profile email"))
    assert result.content == {
        "sub": "user-42",
        "email": "u@example.com",
        "name": "U User",
        "picture": "https://x/p.png",
    }
