"""The admin's "Regenerate secret" action on OAuth2 clients (#193)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skrift.admin.oauth2_clients import OAuth2ClientAdminController
from skrift.auth.client_secret import hash_client_secret
from skrift.db.models.oauth2_client import OAuth2Client


def make_client(**fields) -> OAuth2Client:
    client = OAuth2Client(
        client_id="client-id",
        display_name="Connector",
        redirect_uris="https://example.com/callback",
        allowed_scopes="openid",
        **fields,
    )
    client.id = uuid4()
    return client


async def regenerate(client: OAuth2Client) -> dict:
    """Post "Regenerate secret" for ``client`` and return the session it left."""
    controller = OAuth2ClientAdminController(owner=MagicMock())
    request = MagicMock()
    request.session = {}
    db_session = AsyncMock()
    db_session.execute.return_value.scalar_one_or_none = MagicMock(return_value=client)

    await OAuth2ClientAdminController.regenerate_secret.fn(
        controller, request, db_session, client.id
    )
    return request.session


@pytest.mark.parametrize(
    "auth_method, secret",
    [
        ("client_secret_basic", hash_client_secret("registered-secret")),
        ("none", ""),
    ],
    ids=["confidential", "public"],
)
async def test_a_dynamically_registered_clients_secret_is_left_to_registration(auth_method, secret):
    client = make_client(
        client_secret=secret,
        is_dynamically_registered=True,
        token_endpoint_auth_method=auth_method,
    )

    session = await regenerate(client)

    assert client.client_secret == secret
    assert "new_secret" not in session
    assert session["flash_messages"] == [{
        "message": (
            "Dynamically registered clients receive their credentials when they "
            "register; their secrets are not managed from the admin"
        ),
        "type": "error",
        "dismissible": True,
    }]


async def test_an_admin_created_clients_secret_is_regenerated():
    old_secret = hash_client_secret("admin-secret")
    client = make_client(client_secret=old_secret)

    session = await regenerate(client)

    assert client.client_secret != old_secret
    assert session["new_secret"]
    assert session["flash_messages"][0]["type"] == "success"
