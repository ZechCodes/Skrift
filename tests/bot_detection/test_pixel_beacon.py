"""Tests for the pixel beacon — token signing, metric, and template helper."""

import pytest

from skrift.bot_detection.beacon import (
    PIXEL_LOADED_NS,
    make_pixel_token,
    render_pixel_tag,
    verify_pixel_token,
)
from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.metrics.pixel_beacon import PixelBeaconMetric
from skrift.bot_detection.store import InMemoryBotStateStore


class TestPixelToken:
    def test_round_trip_verification(self):
        token, sig = make_pixel_token("secret")
        assert verify_pixel_token("secret", token, sig) is True

    def test_wrong_secret_fails(self):
        token, sig = make_pixel_token("secret-a")
        assert verify_pixel_token("secret-b", token, sig) is False

    def test_tampered_token_fails(self):
        token, sig = make_pixel_token("secret")
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert verify_pixel_token("secret", tampered, sig) is False

    def test_empty_token_fails(self):
        assert verify_pixel_token("secret", "", "") is False

    def test_each_token_is_unique(self):
        t1, _ = make_pixel_token("secret")
        t2, _ = make_pixel_token("secret")
        assert t1 != t2


class TestRenderPixelTag:
    def test_outputs_img_tag_with_token_and_signature(self):
        html = str(render_pixel_tag("token-abc", "sig-xyz"))
        assert '<img ' in html
        assert "/_bot/p.gif?t=token-abc&s=sig-xyz" in html
        assert 'aria-hidden="true"' in html

    def test_css_beacon_includes_both_tags(self):
        html = str(render_pixel_tag("token", "sig", css_beacon=True))
        assert "/_bot/p.gif" in html
        assert "/_bot/c.gif" in html

    def test_no_css_beacon_omits_css(self):
        html = str(render_pixel_tag("token", "sig", css_beacon=False))
        assert "/_bot/c.gif" not in html


class TestPixelBeaconMetric:
    @pytest.mark.asyncio
    async def test_inconclusive_when_no_pixel_loaded(self):
        metric = PixelBeaconMetric(BotDetectionConfig())
        store = InMemoryBotStateStore()
        scope = _scope("1.2.3.4")
        result = await metric.check(scope, store)
        assert result.verdict is None
        assert result.signals["loaded"].passed is None

    @pytest.mark.asyncio
    async def test_passes_when_pixel_recorded(self):
        metric = PixelBeaconMetric(BotDetectionConfig())
        store = InMemoryBotStateStore()
        await store.set(PIXEL_LOADED_NS, "1.2.3.4", "pixel", ttl=3600)
        result = await metric.check(_scope("1.2.3.4"), store)
        assert result.verdict is True
        assert result.signals["loaded"].passed is True
        assert "pixel" in (result.signals["loaded"].detail or "")

    @pytest.mark.asyncio
    async def test_passes_when_css_beacon_recorded(self):
        metric = PixelBeaconMetric(BotDetectionConfig())
        store = InMemoryBotStateStore()
        await store.set(PIXEL_LOADED_NS, "1.2.3.4", "css", ttl=3600)
        result = await metric.check(_scope("1.2.3.4"), store)
        assert result.verdict is True
        assert "css" in (result.signals["loaded"].detail or "")

    @pytest.mark.asyncio
    async def test_disabled_metric_does_not_run(self):
        config = BotDetectionConfig(pixel_beacon={"enabled": False})
        metric = PixelBeaconMetric(config)
        assert metric.enabled is False


def _scope(ip: str):
    return {
        "type": "http",
        "method": "GET",
        "path": "/page",
        "headers": [],
        "client": ("0.0.0.0", 0),
        "state": {"client_ip": ip},
    }
