"""Tests for the verdict + signal data types."""

from skrift.bot_detection.types import (
    BotDetectionResult,
    MetricResult,
    Signal,
    derive_overall_verdict,
    derive_verdict,
)


class TestDeriveVerdict:
    def test_empty_signals_is_inconclusive(self):
        assert derive_verdict({}) is None

    def test_all_inconclusive_signals_is_inconclusive(self):
        signals = {"a": Signal(None), "b": Signal(None)}
        assert derive_verdict(signals) is None

    def test_any_failed_signal_fails_verdict(self):
        signals = {"a": Signal(True), "b": Signal(False), "c": Signal(None)}
        assert derive_verdict(signals) is False

    def test_all_passing_signals_passes(self):
        signals = {"a": Signal(True), "b": Signal(True)}
        assert derive_verdict(signals) is True

    def test_pass_plus_inconclusive_passes(self):
        signals = {"a": Signal(True), "b": Signal(None)}
        assert derive_verdict(signals) is True


class TestDeriveOverallVerdict:
    def test_empty_metrics_is_inconclusive(self):
        assert derive_overall_verdict({}) is None

    def test_any_failed_metric_fails_overall(self):
        metrics = {
            "a": MetricResult("a", verdict=True, signals={}),
            "b": MetricResult("b", verdict=False, signals={}),
        }
        assert derive_overall_verdict(metrics) is False

    def test_all_passing_metrics_pass_overall(self):
        metrics = {
            "a": MetricResult("a", verdict=True, signals={}),
            "b": MetricResult("b", verdict=True, signals={}),
        }
        assert derive_overall_verdict(metrics) is True

    def test_all_inconclusive_metrics_is_inconclusive(self):
        metrics = {"a": MetricResult("a", verdict=None, signals={})}
        assert derive_overall_verdict(metrics) is None


class TestSignalAndResultStructures:
    def test_signal_has_optional_detail(self):
        s = Signal(False, detail="Puppeteer detected")
        assert s.passed is False
        assert s.detail == "Puppeteer detected"

    def test_metric_result_carries_named_signals(self):
        m = MetricResult(
            "headless_ua",
            verdict=False,
            signals={"puppeteer": Signal(False, "UA contains 'Puppeteer'")},
        )
        assert m.name == "headless_ua"
        assert m.signals["puppeteer"].passed is False

    def test_bot_detection_result_aggregates_metrics(self):
        result = BotDetectionResult(
            verdict=False,
            metrics={"headless_ua": MetricResult("headless_ua", False, {})},
        )
        assert result.verdict is False
        assert "headless_ua" in result.metrics
