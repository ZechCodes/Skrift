"""M2 — refresh grant scope must be a subset of the originally granted scope.

- Absent/empty `scope` form field → reuse the original grant.
- Subset → downgrade (allowed, returned scope matches request).
- Superset or unknown scope → `invalid_scope` 400.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.auth.tokens import create_signed_token
from skrift.controllers.oauth2 import (
    OAuth2Controller,
    REFRESH_TOKEN_TTL,
)

SECRET = "test-secret-key"


def _settings():
    settings = MagicMock()
    settings.secret_key = SECRET
    return settings


def _client():
    from skrift.auth.client_secret import hash_client_secret

    client = MagicMock()
    client.client_secret = hash_client_secret("secret")
    client.redirect_uri_list = ["http://localhost/cb"]
    client.allowed_scope_list = []
    return client


def _refresh_token(scope: str, *, family_id: str = "fam-scope") -> str:
    return create_signed_token(
        {
            "type": "refresh",
            "user_id": "u",
            "client_id": "abc",
            "scope": scope,
            "family_id": family_id,
        },
        SECRET,
        REFRESH_TOKEN_TTL,
    )


def _request(refresh: str, *, scope: str | None = None):
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "abc",
        "client_secret": "secret",
    }
    if scope is not None:
        data["scope"] = scope
    request = MagicMock()
    request.form = AsyncMock(return_value=data)
    return request


async def _exchange(controller, request, db_session, *, client):
    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
        mock_svc.is_token_revoked = AsyncMock(return_value=False)
        mock_svc.is_family_revoked = AsyncMock(return_value=False)
        mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
        mock_svc.revoke_token = AsyncMock()
        mock_svc.revoke_family = AsyncMock()
        return await OAuth2Controller.token_exchange.fn(controller, request, db_session)


@pytest.mark.asyncio
async def test_absent_scope_preserves_original_grant():
    controller = OAuth2Controller(owner=MagicMock())
    result = await _exchange(
        controller,
        _request(_refresh_token("openid profile email")),
        AsyncMock(),
        client=_client(),
    )
    assert result.status_code == 200
    assert result.content["scope"] == "openid profile email"


@pytest.mark.asyncio
async def test_subset_scope_downgrades_without_error():
    controller = OAuth2Controller(owner=MagicMock())
    result = await _exchange(
        controller,
        _request(_refresh_token("openid profile email"), scope="openid"),
        AsyncMock(),
        client=_client(),
    )
    assert result.status_code == 200
    # Returned scope is the downgraded subset, normalized (sorted).
    assert result.content["scope"] == "openid"


@pytest.mark.asyncio
async def test_identical_scope_passes_through():
    controller = OAuth2Controller(owner=MagicMock())
    result = await _exchange(
        controller,
        _request(_refresh_token("openid email"), scope="email openid"),
        AsyncMock(),
        client=_client(),
    )
    assert result.status_code == 200
    assert result.content["scope"] == "email openid"


@pytest.mark.asyncio
async def test_superset_scope_rejected_as_invalid_scope():
    """Client must not be able to silently escalate scope on refresh — an
    attacker with a leaked refresh token could otherwise obtain elevated
    access."""
    controller = OAuth2Controller(owner=MagicMock())
    result = await _exchange(
        controller,
        _request(_refresh_token("openid"), scope="openid email"),
        AsyncMock(),
        client=_client(),
    )
    assert result.status_code == 400
    assert result.content["error"] == "invalid_scope"


@pytest.mark.asyncio
async def test_unknown_scope_rejected():
    """Any scope not in the original grant — known or not — is rejected."""
    controller = OAuth2Controller(owner=MagicMock())
    result = await _exchange(
        controller,
        _request(_refresh_token("openid"), scope="admin"),
        AsyncMock(),
        client=_client(),
    )
    assert result.status_code == 400
    assert result.content["error"] == "invalid_scope"
