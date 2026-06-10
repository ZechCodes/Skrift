"""Tests for the optional republish component."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from litestar.response import Redirect

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.republish import REPUBLISH_PERMISSION
from skrift.republish.controller import AccountRepublishController, _validate_canonical_url
from skrift.republish.hooks import (
    add_account_context,
    add_account_section,
    add_grant_constraints,
    add_grant_context,
)


def _settings():
    return SimpleNamespace(
        republish=SimpleNamespace(
            enabled=True,
            discovery_enabled=True,
            default_page_type="post",
            page_types=["post", "note"],
            default_post_behavior="draft",
            default_delete_behavior="unpublish",
        )
    )


@pytest.mark.asyncio
async def test_republish_grant_context_lists_configured_choices():
    context = {}
    grant_data = {"permissions": [REPUBLISH_PERMISSION]}

    with patch("skrift.republish.hooks.get_settings", return_value=_settings()):
        result = await add_grant_context(context, MagicMock(), grant_data)

    assert result["republish_options"]["page_types"] == ["note", "post"]
    assert result["republish_options"]["default_delete_behavior"] == "unpublish"


@pytest.mark.asyncio
async def test_republish_grant_constraints_derive_source_origin_from_service_url():
    grant_data = {
        "permissions": [REPUBLISH_PERMISSION],
        "service_url": "https://source.example/app",
        "redirect_uri": "https://callback.example/oauth",
    }
    form_data = {
        "republish_page_type": "post",
        "republish_post_behavior": "publish",
        "republish_delete_behavior": "unpublish",
    }

    with patch("skrift.republish.hooks.get_settings", return_value=_settings()):
        result = await add_grant_constraints({}, MagicMock(), grant_data, form_data)

    assert result["republish"] == {
        "schema": "baseline-v1",
        "source_origin": "https://source.example",
        "page_type": "post",
        "post_behavior": "publish",
        "delete_behavior": "unpublish",
    }


def test_validate_canonical_url_enforces_source_origin():
    result = _validate_canonical_url(
        {"canonical_url": "https://other.example/post/1"},
        {"source_origin": "https://source.example"},
    )

    assert result.status_code == 403
    assert result.content["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_republish_account_context_lists_links():
    user = MagicMock()
    user.id = uuid4()
    outbound = [MagicMock()]
    inbound = [{"source_origin": "https://source.example", "post_count": 2}]
    context = {}

    with patch(
        "skrift.republish.hooks.list_outbound_links",
        new=AsyncMock(return_value=outbound),
    ), patch(
        "skrift.republish.hooks.list_inbound_publishers",
        new=AsyncMock(return_value=inbound),
    ):
        result = await add_account_context(context, MagicMock(), MagicMock(), user)

    assert result["republish_account"]["outbound_links"] == outbound
    assert result["republish_account"]["inbound_publishers"] == inbound


@pytest.mark.asyncio
async def test_republish_account_section_adds_template():
    result = await add_account_section([], MagicMock(), MagicMock(), MagicMock(), {})

    assert result == [{"template": "republish/account_section.html"}]


@pytest.mark.asyncio
async def test_create_republish_link_saves_destination():
    controller = AccountRepublishController(owner=MagicMock())
    request = MagicMock()
    request.session = {SESSION_USER_ID: str(uuid4())}

    with patch("skrift.republish.controller.get_settings", return_value=_settings()), patch(
        "skrift.republish.controller.verify_csrf",
        new=AsyncMock(return_value=True),
    ), patch(
        "skrift.republish.controller.save_outbound_link",
        new=AsyncMock(),
    ) as save_link:
        result = await AccountRepublishController.create_link.fn(
            controller,
            request,
            MagicMock(),
            {"site_url": "https://target.example/path", "site_name": "Target"},
        )

    assert isinstance(result, Redirect)
    assert result.url == "/account"
    save_link.assert_awaited_once()
    assert save_link.await_args.kwargs["site_url"] == "https://target.example/path"
    assert save_link.await_args.kwargs["site_name"] == "Target"
