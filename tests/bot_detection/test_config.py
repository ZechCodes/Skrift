"""Tests for BotDetectionConfig and Settings wiring."""

from skrift.bot_detection.config import (
    BotDetectionConfig,
    HeadlessUAConfig,
    JSChallengeConfig,
    PixelBeaconConfig,
    RobotsHoneypotConfig,
)


class TestBotDetectionConfigDefaults:
    def test_disabled_by_default(self):
        config = BotDetectionConfig()
        assert config.enabled is False

    def test_default_skip_paths(self):
        config = BotDetectionConfig()
        assert "/static/" in config.skip_paths
        assert "/_bot/" in config.skip_paths

    def test_default_legitimate_bots(self):
        config = BotDetectionConfig()
        assert "Googlebot" in config.legitimate_bot_uas

    def test_each_metric_enabled_by_default_except_js_challenge(self):
        config = BotDetectionConfig()
        assert config.headless_ua.enabled is True
        assert config.header_coherence.enabled is True
        assert config.direct_request.enabled is True
        assert config.pixel_beacon.enabled is True
        assert config.robots_honeypot.enabled is True
        # JS challenge is opt-in because it breaks no-JS clients.
        assert config.js_challenge.enabled is False


class TestPerMetricConfig:
    def test_headless_ua_has_default_patterns(self):
        config = HeadlessUAConfig()
        assert "HeadlessChrome" in config.patterns
        assert "Puppeteer" in config.patterns

    def test_pixel_beacon_defaults(self):
        config = PixelBeaconConfig()
        assert config.cache_ttl == 60
        assert config.inject_pixel is True
        assert config.inject_css_beacon is True

    def test_robots_honeypot_defaults(self):
        config = RobotsHoneypotConfig()
        assert config.trap_path == "/private-area"
        assert config.rotate_token_days == 7

    def test_js_challenge_defaults(self):
        config = JSChallengeConfig()
        assert config.block_unverified_after == 3
        assert config.challenge_ttl == 3600


class TestNestedConfigParsing:
    def test_can_construct_with_nested_dicts(self):
        config = BotDetectionConfig(
            enabled=True,
            headless_ua={"enabled": False, "patterns": ["FooBot"]},
            pixel_beacon={"cache_ttl": 120},
        )
        assert config.enabled is True
        assert config.headless_ua.enabled is False
        assert config.headless_ua.patterns == ["FooBot"]
        assert config.pixel_beacon.cache_ttl == 120


class TestSettingsIntegration:
    def test_settings_has_bot_detection(self):
        from skrift.config import Settings

        # Settings inherits BaseSettings; secret_key is required from env
        # in production, so build directly with a stub secret_key.
        s = Settings(secret_key="test")
        assert isinstance(s.bot_detection, BotDetectionConfig)
        assert s.bot_detection.enabled is False
