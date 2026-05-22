"""Tests for API permission grant flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from litestar.response import Template as TemplateResponse

from skrift.auth.permissions import (
    ALLOW_ANONYMOUS_SERVICE,
    DISALLOW_API_GRANTS,
    PERMISSION_DEFINITIONS,
    REQUIRE_KNOWN_SERVICE,
    get_permission_definition,
    register_permission,
)
from skrift.auth.tokens import create_signed_token, verify_signed_token
from skrift.controllers.api_grants import APIGrantController


SECRET = "api-grant-test-secret"


def _settings():
    return SimpleNamespace(
        secret_key=SECRET,
        api_keys=SimpleNamespace(
            default_expiration_days=365,
            refresh_token_expiration_days=30,
        ),
    )


def _request(*, json_body=None, form_data=None, headers=None, session=None, query_params=None):
    request = MagicMock()
    request.headers = headers or {}
    request.scope = {"client": ("127.0.0.1", 1234)}
    request.session = session if session is not None else {}
    request.query_params = query_params or {}
    if json_body is not None:
        request.json = AsyncMock(return_value=json_body)
    if form_data is not None:
        request.form = AsyncMock(return_value=form_data)
    return request


@pytest.fixture(autouse=True)
def restore_permission_registry():
    original = dict(PERMISSION_DEFINITIONS)
    yield
    PERMISSION_DEFINITIONS.clear()
    PERMISSION_DEFINITIONS.update(original)


def test_unknown_permissions_default_to_disallow_api_grants():
    definition = get_permission_definition("custom-sensitive-action")

    assert definition.service_clearance == DISALLOW_API_GRANTS


@pytest.mark.asyncio
async def test_anonymous_grant_request_requires_service_name():
    register_permission(
        "read-public-data",
        display_name="Read Public Data",
        service_clearance=ALLOW_ANONYMOUS_SERVICE,
    )
    controller = APIGrantController(owner=MagicMock())
    request = _request(
        json_body={
            "permissions": ["read-public-data"],
            "redirect_uri": "https://service.example/callback",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        }
    )

    result = await APIGrantController.create_request.fn(controller, request, AsyncMock())

    assert result.status_code == 400
    assert result.content["error_description"] == "service_name is required"


@pytest.mark.asyncio
async def test_known_service_request_must_be_subset_of_service_key():
    register_permission(
        "read-records",
        display_name="Read Records",
        service_clearance=REQUIRE_KNOWN_SERVICE,
    )
    controller = APIGrantController(owner=MagicMock())
    request = _request(
        headers={"authorization": "Bearer sk_parent"},
        json_body={
            "permissions": ["read-records"],
            "redirect_uri": "https://service.example/callback",
            "service_name": "Service",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
    )
    parent_key = MagicMock()
    parent_key.id = uuid4()
    parent_key.principal_type = "service"
    parent_permissions = SimpleNamespace(permissions=set())

    with patch(
        "skrift.controllers.api_grants.api_key_service.verify_api_key_with_permissions",
        new=AsyncMock(return_value=(parent_key, parent_permissions)),
    ):
        result = await APIGrantController.create_request.fn(controller, request, AsyncMock())

    assert result.status_code == 403
    assert result.content["error"] == "invalid_scope"


@pytest.mark.asyncio
async def test_anonymous_request_with_service_key_does_not_bind_parent_key():
    register_permission(
        "read-public-data",
        display_name="Read Public Data",
        service_clearance=ALLOW_ANONYMOUS_SERVICE,
    )
    controller = APIGrantController(owner=MagicMock())
    request = _request(
        headers={"authorization": "Bearer sk_parent"},
        json_body={
            "permissions": ["read-public-data"],
            "redirect_uri": "https://service.example/callback",
            "service_name": "Service",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
    )
    parent_key = MagicMock()
    parent_key.id = uuid4()
    parent_key.principal_type = "service"
    parent_permissions = SimpleNamespace(permissions=set())

    with patch("skrift.controllers.api_grants.get_settings", return_value=_settings()), patch(
        "skrift.controllers.api_grants.api_key_service.verify_api_key_with_permissions",
        new=AsyncMock(return_value=(parent_key, parent_permissions)),
    ):
        result = await APIGrantController.create_request.fn(controller, request, AsyncMock())

    payload = verify_signed_token(result.content["request_token"], SECRET)
    assert result.status_code == 200
    assert payload["parent_api_key_id"] == ""


@pytest.mark.asyncio
async def test_known_service_request_returns_request_token_for_valid_subset():
    register_permission(
        "read-records",
        display_name="Read Records",
        service_clearance=REQUIRE_KNOWN_SERVICE,
    )
    controller = APIGrantController(owner=MagicMock())
    request = _request(
        headers={"authorization": "Bearer sk_parent"},
        json_body={
            "permissions": ["read-records"],
            "redirect_uri": "https://service.example/callback",
            "service_name": "Service",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
    )
    parent_key = MagicMock()
    parent_key.id = uuid4()
    parent_key.principal_type = "service"
    parent_key.service_name = "Service"
    parent_key.service_url = ""
    parent_permissions = SimpleNamespace(permissions={"read-records"})

    with patch(
        "skrift.controllers.api_grants.get_settings",
        return_value=_settings(),
    ), patch(
        "skrift.controllers.api_grants.api_key_service.verify_api_key_with_permissions",
        new=AsyncMock(return_value=(parent_key, parent_permissions)),
    ):
        result = await APIGrantController.create_request.fn(controller, request, AsyncMock())

    assert result.status_code == 200
    assert result.content["request_token"]
    assert result.content["authorize_url"].startswith("/api/grants/authorize?")


@pytest.mark.asyncio
async def test_known_service_direct_authorize_returns_friendly_error_page():
    register_permission(
        "read-records",
        display_name="Read Records",
        service_clearance=REQUIRE_KNOWN_SERVICE,
    )
    controller = APIGrantController(owner=MagicMock())
    request = _request(
        query_params={
            "response_type": "code",
            "redirect_uri": "https://service.example/callback",
            "service_url": "https://service.example",
            "permissions": "read-records",
            "service_name": "Service",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        }
    )

    result = await APIGrantController.authorize_get.fn(controller, request, AsyncMock())

    assert isinstance(result, TemplateResponse)
    assert result.template_name == "api_grants/error.html"
    assert result.context["title"] == "Unknown Service"
    assert result.context["kind"] == "unknown_service"
    assert result.context["service_name"] == "Service"
    assert result.context["blocked_permissions"][0]["display_name"] == "Read Records"
    assert "signed request-token flow" in result.context["developer_detail"]
    assert result.context["return_url"] == "https://service.example"


@pytest.mark.asyncio
async def test_token_exchange_issues_user_bound_service_key():
    register_permission(
        "read-public-data",
        display_name="Read Public Data",
        service_clearance=ALLOW_ANONYMOUS_SERVICE,
    )
    controller = APIGrantController(owner=MagicMock())
    code = create_signed_token(
        {
            "type": "api_grant_code",
            "user_id": str(uuid4()),
            "redirect_uri": "https://service.example/callback",
            "permissions": "read-public-data",
            "service_name": "Service",
            "service_url": "https://service.example",
            "service_clearance": ALLOW_ANONYMOUS_SERVICE,
            "parent_api_key_id": "",
            "code_challenge": "6eNLsNmDG0_7Yp-5b8dhj4zWXx5Zt3M4B7c-M7dDht0",
        },
        SECRET,
        600,
    )
    request = _request(
        form_data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://service.example/callback",
            "code_verifier": "verifier",
        }
    )
    api_key = MagicMock()
    api_key.key_prefix = "sk_testpref"
    api_key.expires_at = None
    api_key.refresh_token_expires_at = None
    api_key.principal_type = "service"
    api_key.service_name = "Service"

    with patch("skrift.controllers.api_grants.get_settings", return_value=_settings()), patch(
        "skrift.controllers.api_grants._verify_pkce",
        return_value=True,
    ), patch(
        "skrift.controllers.api_grants.oauth2_service.is_token_revoked",
        new=AsyncMock(return_value=False),
    ), patch(
        "skrift.controllers.api_grants.oauth2_service.revoke_token",
        new=AsyncMock(),
    ), patch(
        "skrift.controllers.api_grants.api_key_service.create_api_key",
        new=AsyncMock(return_value=(api_key, "sk_raw", "skr_refresh")),
    ) as create_key:
        result = await APIGrantController.token_exchange.fn(controller, request, AsyncMock())

    assert result.status_code == 200
    assert result.content["key"] == "sk_raw"
    assert result.content["principal_type"] == "service"
    assert result.content["permissions"] == ["read-public-data"]
    assert create_key.await_args.kwargs["principal_type"] == "service"
    assert create_key.await_args.kwargs["scoped_permissions"] == ["read-public-data"]
    assert create_key.await_args.kwargs["grant_source"] == "api-grant"
