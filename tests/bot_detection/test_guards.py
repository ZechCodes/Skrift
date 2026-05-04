"""Tests for the BotGuard Litestar route guard."""

from unittest.mock import MagicMock

import pytest
from litestar.exceptions import PermissionDeniedException

from skrift.bot_detection.guards import BotGuard
from skrift.bot_detection.types import BotDetectionResult, MetricResult, Signal


def make_connection(result: BotDetectionResult | None):
    """Build a minimal ASGIConnection-like stand-in with the desired state."""
    state = {} if result is None else {"bot_detection": result}
    connection = MagicMock()
    connection.scope = {"state": state}
    return connection


class TestBotGuardDefaultMode:
    def test_passes_when_verdict_true(self):
        result = BotDetectionResult(verdict=True, metrics={})
        BotGuard()(make_connection(result), MagicMock())

    def test_blocks_when_verdict_false(self):
        result = BotDetectionResult(verdict=False, metrics={})
        with pytest.raises(PermissionDeniedException):
            BotGuard()(make_connection(result), MagicMock())

    def test_allows_unknown_when_on_unknown_allow(self):
        result = BotDetectionResult(verdict=None, metrics={})
        BotGuard(on_unknown="allow")(make_connection(result), MagicMock())

    def test_blocks_unknown_when_on_unknown_deny(self):
        result = BotDetectionResult(verdict=None, metrics={})
        with pytest.raises(PermissionDeniedException):
            BotGuard(on_unknown="deny")(make_connection(result), MagicMock())

    def test_no_middleware_state_passes_through(self):
        BotGuard()(make_connection(None), MagicMock())


class TestBotGuardSignalMode:
    def _result(self, **signals):
        metric = MetricResult("headless_ua", verdict=True, signals=signals)
        return BotDetectionResult(verdict=True, metrics={"headless_ua": metric})

    def test_passes_when_required_signal_passes(self):
        result = self._result(puppeteer=Signal(True))
        BotGuard(require_signals=["headless_ua.puppeteer"])(
            make_connection(result), MagicMock()
        )

    def test_blocks_when_required_signal_fails(self):
        result = self._result(puppeteer=Signal(False))
        with pytest.raises(PermissionDeniedException):
            BotGuard(require_signals=["headless_ua.puppeteer"])(
                make_connection(result), MagicMock()
            )

    def test_allows_missing_signal_when_on_unknown_allow(self):
        result = self._result(other=Signal(True))
        BotGuard(
            require_signals=["headless_ua.puppeteer"], on_unknown="allow"
        )(make_connection(result), MagicMock())

    def test_blocks_missing_signal_when_on_unknown_deny(self):
        result = self._result(other=Signal(True))
        with pytest.raises(PermissionDeniedException):
            BotGuard(
                require_signals=["headless_ua.puppeteer"], on_unknown="deny"
            )(make_connection(result), MagicMock())

    def test_blocks_inconclusive_signal_when_on_unknown_deny(self):
        result = self._result(puppeteer=Signal(None))
        with pytest.raises(PermissionDeniedException):
            BotGuard(
                require_signals=["headless_ua.puppeteer"], on_unknown="deny"
            )(make_connection(result), MagicMock())

    def test_blocks_when_metric_missing_entirely(self):
        result = BotDetectionResult(verdict=True, metrics={})
        with pytest.raises(PermissionDeniedException):
            BotGuard(
                require_signals=["headless_ua.puppeteer"], on_unknown="deny"
            )(make_connection(result), MagicMock())

    def test_invalid_reference_raises_value_error(self):
        result = self._result(puppeteer=Signal(True))
        with pytest.raises(ValueError):
            BotGuard(require_signals=["badref"])(
                make_connection(result), MagicMock()
            )

    def test_multiple_required_signals_all_must_pass(self):
        metric = MetricResult(
            "headless_ua",
            verdict=True,
            signals={"a": Signal(True), "b": Signal(False)},
        )
        result = BotDetectionResult(verdict=True, metrics={"headless_ua": metric})
        with pytest.raises(PermissionDeniedException):
            BotGuard(
                require_signals=["headless_ua.a", "headless_ua.b"]
            )(make_connection(result), MagicMock())
