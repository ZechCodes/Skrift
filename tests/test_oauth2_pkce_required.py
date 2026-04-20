"""M3 — PKCE is required for every OAuth2 client.

OAuth 2.1 drops the public/confidential distinction for PKCE: every
authorization request must include `code_challenge` + S256. Skrift's
prior behavior required PKCE only for public (no client_secret) clients.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.controllers.oauth2 import OAuth2Controller


SECRET = "test-secret-key"


def _settings():
    s = MagicMock()
    s.secret_key = SECRET
    return s


def _client(*, has_secret: bool = True, allowed_scopes: list[str] | None = None):
    from skrift.auth.client_secret import hash_client_secret

    client = MagicMock()
    client.client_secret = hash_client_secret("secret") if has_secret else ""
    client.redirect_uri_list = ["http://localhost/cb"]
    client.allowed_scope_list = allowed_scopes or []
    return client


async def _authorize(controller, *, client, query):
    request = MagicMock()
    request.query_params = query
    request.session = {}
    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
        mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
        return await OAuth2Controller.authorize_get.fn(
            controller, request, AsyncMock()
        )


def _base_params(**overrides):
    params = {
        "client_id": "abc",
        "redirect_uri": "http://localhost/cb",
        "response_type": "code",
        "state": "s",
        "scope": "",
        "code_challenge": "",
        "code_challenge_method": "",
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_confidential_client_without_pkce_rejected():
    """Previously this was the path that skipped PKCE — now it must fail."""
    controller = OAuth2Controller(owner=MagicMock())
    result = await _authorize(
        controller,
        client=_client(has_secret=True),
        query=_base_params(code_challenge=""),
    )
    assert result.status_code == 400
    assert result.content["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_public_client_without_pkce_rejected():
    """Already rejected before this change; pinned here so a future
    refactor can't silently flip the default."""
    controller = OAuth2Controller(owner=MagicMock())
    result = await _authorize(
        controller,
        client=_client(has_secret=False),
        query=_base_params(code_challenge=""),
    )
    assert result.status_code == 400
    assert result.content["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_pkce_challenge_without_method_rejected():
    controller = OAuth2Controller(owner=MagicMock())
    result = await _authorize(
        controller,
        client=_client(has_secret=True),
        query=_base_params(code_challenge="abc123", code_challenge_method=""),
    )
    assert result.status_code == 400
    assert result.content["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_pkce_method_plain_rejected():
    """S256 is the only accepted method (no `plain`)."""
    controller = OAuth2Controller(owner=MagicMock())
    result = await _authorize(
        controller,
        client=_client(has_secret=True),
        query=_base_params(
            code_challenge="abc123", code_challenge_method="plain"
        ),
    )
    assert result.status_code == 400
    assert result.content["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_token_grant_rejects_code_without_challenge():
    """If an auth code somehow lacked a `code_challenge` claim (it
    shouldn't, because `authorize_get` always stamps one), the token
    endpoint refuses rather than silently granting without PKCE."""
    from skrift.auth.tokens import create_signed_token
    from skrift.controllers.oauth2 import AUTH_CODE_TTL

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
            "code_challenge": "",  # deliberately missing
        },
        SECRET,
        AUTH_CODE_TTL,
    )

    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.form = AsyncMock(
        return_value={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/cb",
            "client_id": "abc",
            "client_secret": "secret",
            "code_verifier": "anything",
        }
    )
    db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
        mock_svc.get_client_by_client_id = AsyncMock(return_value=_client())
        mock_svc.is_token_revoked = AsyncMock(return_value=False)
        mock_svc.revoke_token = AsyncMock()
        result = await OAuth2Controller.token_exchange.fn(
            controller, request, db_session
        )

    assert result.status_code == 400
    assert result.content["error"] == "invalid_grant"
