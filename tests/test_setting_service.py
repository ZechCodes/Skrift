"""The site settings service's fallbacks swallow their failures (#190)."""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from skrift.db.services import setting_service


@pytest.fixture(autouse=True)
def empty_settings_cache():
    setting_service.invalidate_site_settings_cache()
    yield
    setting_service.invalidate_site_settings_cache()


async def test_a_settings_cache_that_cannot_load_is_left_empty(caplog):
    # For example before the migrations have created the settings table.
    failing = AsyncMock(side_effect=RuntimeError("no such table: settings"))
    with patch.object(setting_service, "get_settings", failing), caplog.at_level(logging.DEBUG):
        await setting_service.load_site_settings_cache(AsyncMock())

    assert not setting_service.site_settings_cache_loaded()
    assert "Could not load site settings cache" in caplog.text


def test_a_theme_with_no_settings_to_fall_back_to_is_empty(caplog):
    # With nothing cached, the theme falls back to app.yaml, which is missing.
    failing = patch("skrift.config.get_settings", side_effect=FileNotFoundError("app.yaml"))
    with failing, caplog.at_level(logging.DEBUG):
        assert setting_service.get_cached_site_theme() == ""

    assert "Could not fall back to app.yaml theme" in caplog.text
