"""Tests for the core account page."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.controllers.account import AccountController
from skrift.lib.hooks import ACCOUNT_PAGE_CONTEXT, ACCOUNT_PAGE_SECTIONS, hooks


@pytest.mark.asyncio
async def test_account_page_renders_hook_sections(clean_hooks):
    user = SimpleNamespace(id="user-id", email="user@example.com", name="User")
    controller = AccountController(owner=MagicMock())
    request = MagicMock()

    async def add_context(context, request, db_session, user):
        context["custom_account"] = {"enabled": True}
        return context

    async def add_section(sections, request, db_session, user, context):
        sections.append({"template": "custom/account_section.html"})
        return sections

    hooks.add_filter(ACCOUNT_PAGE_CONTEXT, add_context)
    hooks.add_filter(ACCOUNT_PAGE_SECTIONS, add_section)

    with patch(
        "skrift.controllers.account.get_user_context",
        new=AsyncMock(return_value={"user": user}),
    ), patch(
        "skrift.controllers.account.get_flash_messages",
        return_value=[],
    ):
        result = await AccountController.account_index.fn(controller, request, AsyncMock())

    assert result.template_name == "account/index.html"
    assert result.context["custom_account"] == {"enabled": True}
    assert result.context["account_sections"] == [
        {"template": "custom/account_section.html"}
    ]
