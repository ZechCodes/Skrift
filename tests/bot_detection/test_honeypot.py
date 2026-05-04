"""Tests for the robots honeypot — token rotation, rule injection, metric."""

from unittest.mock import patch

import pytest

from skrift.bot_detection.config import BotDetectionConfig, RobotsHoneypotConfig
from skrift.bot_detection.honeypot import (
    inject_disallow_rule,
    is_trap_path,
    make_trap_token,
    trap_url,
)
from skrift.bot_detection.metrics.robots_honeypot import RobotsHoneypotMetric
from skrift.bot_detection.store import InMemoryBotStateStore


class TestMakeTrapToken:
    def test_token_is_stable_within_period(self):
        with patch("skrift.bot_detection.honeypot.time.time", return_value=1_700_000_000):
            t1 = make_trap_token("secret", rotate_token_days=7)
            t2 = make_trap_token("secret", rotate_token_days=7)
        assert t1 == t2

    def test_token_changes_when_period_advances(self):
        seven_days = 7 * 86400
        with patch("skrift.bot_detection.honeypot.time.time", return_value=1_700_000_000):
            t1 = make_trap_token("secret", rotate_token_days=7)
        with patch(
            "skrift.bot_detection.honeypot.time.time",
            return_value=1_700_000_000 + seven_days * 2,
        ):
            t2 = make_trap_token("secret", rotate_token_days=7)
        assert t1 != t2

    def test_token_differs_per_secret(self):
        with patch("skrift.bot_detection.honeypot.time.time", return_value=1_700_000_000):
            t1 = make_trap_token("secret-a", rotate_token_days=7)
            t2 = make_trap_token("secret-b", rotate_token_days=7)
        assert t1 != t2

    def test_token_is_url_safe(self):
        token = make_trap_token("secret", rotate_token_days=7)
        # base64.urlsafe charset.
        assert all(
            c.isalnum() or c in "-_=" for c in token
        )


class TestTrapUrl:
    def test_trap_url_combines_prefix_and_token(self):
        cfg = RobotsHoneypotConfig(trap_path="/private-area")
        url = trap_url(cfg, "secret")
        assert url.startswith("/private-area/")
        assert len(url) > len("/private-area/")

    def test_trap_url_strips_trailing_slash(self):
        cfg = RobotsHoneypotConfig(trap_path="/private-area/")
        url = trap_url(cfg, "secret")
        # No double slash.
        assert "//" not in url


class TestIsTrapPath:
    def test_exact_prefix_matches(self):
        cfg = RobotsHoneypotConfig(trap_path="/private-area")
        assert is_trap_path("/private-area", cfg) is True

    def test_sub_path_matches(self):
        cfg = RobotsHoneypotConfig(trap_path="/private-area")
        assert is_trap_path("/private-area/abc123", cfg) is True

    def test_unrelated_path_does_not_match(self):
        cfg = RobotsHoneypotConfig(trap_path="/private-area")
        assert is_trap_path("/public", cfg) is False

    def test_partial_prefix_does_not_match(self):
        cfg = RobotsHoneypotConfig(trap_path="/private-area")
        assert is_trap_path("/private-areas", cfg) is False  # different path

    def test_empty_trap_path_never_matches(self):
        cfg = RobotsHoneypotConfig(trap_path="")
        assert is_trap_path("/anything", cfg) is False


class TestInjectDisallowRule:
    def test_appends_to_existing_user_agent_block(self):
        content = "User-agent: *\nAllow: /\n\nSitemap: https://example.com/sitemap.xml\n"
        result = inject_disallow_rule(content, "/private-area/abc")
        assert "Disallow: /private-area/abc" in result
        # The injected rule should appear before the Allow line (right after User-agent: *).
        ua_idx = result.index("User-agent: *")
        rule_idx = result.index("Disallow: /private-area/abc")
        assert rule_idx > ua_idx

    def test_prepends_when_no_user_agent_block(self):
        content = "# empty file\n"
        result = inject_disallow_rule(content, "/private-area/abc")
        assert result.startswith("User-agent: *\n")
        assert "Disallow: /private-area/abc" in result

    def test_idempotent_when_rule_already_present(self):
        content = "User-agent: *\nDisallow: /private-area/abc\n"
        result = inject_disallow_rule(content, "/private-area/abc")
        # Should not be added a second time.
        assert result.count("Disallow: /private-area/abc") == 1


class TestRobotsHoneypotMetric:
    @pytest.mark.asyncio
    async def test_inconclusive_when_no_state(self):
        metric = RobotsHoneypotMetric(BotDetectionConfig())
        store = InMemoryBotStateStore()
        scope = _scope("1.2.3.4")
        result = await metric.check(scope, store)
        assert result.verdict is None

    @pytest.mark.asyncio
    async def test_passes_when_robots_read_no_trap(self):
        from skrift.bot_detection.honeypot import ROBOTS_READ_NS

        metric = RobotsHoneypotMetric(BotDetectionConfig())
        store = InMemoryBotStateStore()
        await store.set(ROBOTS_READ_NS, "1.2.3.4", "1", ttl=3600)
        result = await metric.check(_scope("1.2.3.4"), store)
        assert result.verdict is True
        assert result.signals["trap_compliance"].passed is True
        assert result.signals["robots_txt_aware"].passed is True

    @pytest.mark.asyncio
    async def test_fails_when_trap_hit_and_robots_read(self):
        from skrift.bot_detection.honeypot import (
            ROBOTS_READ_NS,
            TRAP_HIT_NS,
        )

        metric = RobotsHoneypotMetric(BotDetectionConfig())
        store = InMemoryBotStateStore()
        await store.set(ROBOTS_READ_NS, "1.2.3.4", "1", ttl=3600)
        await store.set(TRAP_HIT_NS, "1.2.3.4", "/private-area/x", ttl=3600)
        result = await metric.check(_scope("1.2.3.4"), store)
        assert result.verdict is False
        assert "non-compliant" in (
            result.signals["trap_compliance"].detail or ""
        )

    @pytest.mark.asyncio
    async def test_fails_when_trap_hit_and_robots_not_read(self):
        from skrift.bot_detection.honeypot import TRAP_HIT_NS

        metric = RobotsHoneypotMetric(BotDetectionConfig())
        store = InMemoryBotStateStore()
        await store.set(TRAP_HIT_NS, "1.2.3.4", "/private-area/x", ttl=3600)
        result = await metric.check(_scope("1.2.3.4"), store)
        assert result.verdict is False
        assert "naive scraper" in (
            result.signals["trap_compliance"].detail or ""
        )


def _scope(ip: str):
    return {
        "type": "http",
        "method": "GET",
        "path": "/page",
        "headers": [],
        "client": ("0.0.0.0", 0),
        "state": {"client_ip": ip},
    }
