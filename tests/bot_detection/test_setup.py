"""Tests for the bot_detection setup hooks (ROBOTS_TXT filter, action handler)."""

import pytest

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.honeypot import ROBOTS_READ_NS
from skrift.bot_detection.setup import setup_honeypot_hooks
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.lib.hooks import ROBOTS_TXT, ROBOTS_TXT_FETCHED, hooks


@pytest.fixture(autouse=True)
def clean_hooks_each_test():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


class TestSetupHoneypotHooks:
    @pytest.mark.asyncio
    async def test_robots_txt_filter_injects_disallow(self):
        config = BotDetectionConfig(enabled=True)
        store = InMemoryBotStateStore()
        setup_honeypot_hooks(config, store, "test-secret")

        original = "User-agent: *\nAllow: /\n\nSitemap: https://example.com/sitemap.xml\n"
        modified = await hooks.apply_filters(ROBOTS_TXT, original)
        assert "Disallow: /private-area/" in modified

    @pytest.mark.asyncio
    async def test_does_nothing_when_component_disabled(self):
        config = BotDetectionConfig(enabled=False)
        store = InMemoryBotStateStore()
        setup_honeypot_hooks(config, store, "test-secret")

        # No filter registered.
        assert not hooks.has_filter(ROBOTS_TXT)
        assert not hooks.has_action(ROBOTS_TXT_FETCHED)

    @pytest.mark.asyncio
    async def test_does_nothing_when_honeypot_disabled(self):
        config = BotDetectionConfig(
            enabled=True,
            robots_honeypot={"enabled": False},
        )
        store = InMemoryBotStateStore()
        setup_honeypot_hooks(config, store, "test-secret")

        assert not hooks.has_filter(ROBOTS_TXT)
        assert not hooks.has_action(ROBOTS_TXT_FETCHED)

    @pytest.mark.asyncio
    async def test_robots_fetched_action_records_state(self):
        config = BotDetectionConfig(enabled=True)
        store = InMemoryBotStateStore()
        setup_honeypot_hooks(config, store, "test-secret")

        await hooks.do_action(ROBOTS_TXT_FETCHED, None, "1.2.3.4", "Curl/8")
        assert await store.get(ROBOTS_READ_NS, "1.2.3.4") == "1"

    @pytest.mark.asyncio
    async def test_log_robots_fetches_can_be_disabled(self):
        config = BotDetectionConfig(
            enabled=True,
            robots_honeypot={"log_robots_fetches": False},
        )
        store = InMemoryBotStateStore()
        setup_honeypot_hooks(config, store, "test-secret")

        # Filter still registered (we still inject the rule)
        assert hooks.has_filter(ROBOTS_TXT)
        # Action registered but should not record when disabled
        await hooks.do_action(ROBOTS_TXT_FETCHED, None, "1.2.3.4", "Curl/8")
        assert await store.get(ROBOTS_READ_NS, "1.2.3.4") is None
