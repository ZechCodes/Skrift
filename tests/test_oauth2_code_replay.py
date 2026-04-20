"""C3: authorization codes must be single-use.

A replay of a previously-consumed code (within its 10-minute TTL) must fail
with ``invalid_grant`` rather than succeed silently as it did when the code
endpoint skipped revocation tracking.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.auth.tokens import create_signed_token
from skrift.controllers.oauth2 import ACCESS_TOKEN_TTL, AUTH_CODE_TTL, OAuth2Controller


SECRET = "test-secret-key"


def _settings():
    settings = MagicMock()
    settings.secret_key = SECRET
    return settings


def _client(secret="s"):
    """Mock OAuth2Client row.

    ``secret`` is the plaintext the test form submits; the mock stores the
    hashed form so the controller's ``verify_client_secret`` call matches.
    """
    from skrift.auth.client_secret import hash_client_secret

    client = MagicMock()
    client.client_secret = hash_client_secret(secret) if secret else ""
    client.redirect_uri_list = ["http://localhost/cb"]
    return client


def _request(code: str, *, client_secret: str = "s"):
    request = MagicMock()
    request.form = AsyncMock(
        return_value={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/cb",
            "client_id": "abc",
            "client_secret": client_secret,
            "code_verifier": "",
        }
    )
    return request


@pytest.mark.asyncio
async def test_authorization_code_is_revoked_after_successful_exchange():
    code = create_signed_token(
        {
            "type": "code",
            "user_id": "u",
            "email": "a@b.com",
            "name": "n",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "scope": "openid",
            "code_challenge": "",
        },
        SECRET,
        AUTH_CODE_TTL,
    )

    controller = OAuth2Controller(owner=MagicMock())
    request = _request(code)
    db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
        mock_svc.get_client_by_client_id = AsyncMock(return_value=_client())
        mock_svc.is_token_revoked = AsyncMock(return_value=False)
        mock_svc.revoke_token = AsyncMock(return_value=None)

        result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)

    assert result.status_code == 200
    # The code's jti must have been added to revoked_tokens so a replay fails.
    assert mock_svc.revoke_token.await_count == 1
    _sess, jti, ttype, _exp = mock_svc.revoke_token.await_args.args
    assert ttype == "code"
    assert isinstance(jti, str) and jti


@pytest.mark.asyncio
async def test_replaying_a_revoked_code_returns_invalid_grant():
    code = create_signed_token(
        {
            "type": "code",
            "user_id": "u",
            "email": "a@b.com",
            "name": "n",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "scope": "openid",
            "code_challenge": "",
        },
        SECRET,
        AUTH_CODE_TTL,
    )

    controller = OAuth2Controller(owner=MagicMock())
    request = _request(code)
    db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
        mock_svc.get_client_by_client_id = AsyncMock(return_value=_client())
        # Simulate a prior successful exchange by having the revocation check
        # return True on this attempt.
        mock_svc.is_token_revoked = AsyncMock(return_value=True)
        mock_svc.revoke_token = AsyncMock(return_value=None)

        result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)

    assert result.status_code == 400
    assert result.content["error"] == "invalid_grant"
    # The second attempt must not re-revoke or issue tokens.
    mock_svc.revoke_token.assert_not_called()


