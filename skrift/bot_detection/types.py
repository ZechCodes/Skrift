"""Verdict + signal data types for bot detection.

A ``Signal`` is one atomic observation (e.g. "User-Agent contains
'Puppeteer'") with a tri-state outcome: pass (looks human), fail (looks
like a bot), or inconclusive.

A ``MetricResult`` groups the signals produced by a single metric and
exposes a derived verdict.

A ``BotDetectionResult`` aggregates per-metric results into the
top-level shape attached to ``scope["state"]["bot_detection"]``.

End users consume the verdict directly when they want a built-in
decision, or read raw signals when they want to roll their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Signal:
    """One atomic pass/fail observation produced by a metric.

    ``passed`` is tri-state: ``True`` = looks human, ``False`` = looks
    like a bot, ``None`` = inconclusive (e.g. waiting on a deferred
    beacon).
    """

    passed: bool | None
    detail: str | None = None


@dataclass(frozen=True)
class MetricResult:
    """The signals produced by a single metric and its derived verdict.

    The verdict is computed by :func:`derive_verdict` over ``signals``
    when the metric does not supply one explicitly.
    """

    name: str
    verdict: bool | None
    signals: Mapping[str, Signal] = field(default_factory=dict)


@dataclass(frozen=True)
class BotDetectionResult:
    """Aggregate result of running every enabled metric for a request.

    ``verdict`` is ``False`` if any metric verdicted ``False``, ``None``
    if every metric verdict is inconclusive, otherwise ``True``.

    Disabled metrics are absent from ``metrics``.
    """

    verdict: bool | None
    metrics: Mapping[str, MetricResult] = field(default_factory=dict)


def derive_verdict(signals: Mapping[str, Signal]) -> bool | None:
    """Compute a metric verdict from its signals.

    Rule: ``False`` if any signal explicitly failed; ``None`` when no
    signal has a definitive outcome; ``True`` otherwise.
    """
    if not signals:
        return None
    saw_pass = False
    for sig in signals.values():
        if sig.passed is False:
            return False
        if sig.passed is True:
            saw_pass = True
    return True if saw_pass else None


def derive_overall_verdict(metrics: Mapping[str, MetricResult]) -> bool | None:
    """Compute the overall verdict across metric results.

    Same rule as :func:`derive_verdict`, applied to per-metric verdicts.
    """
    if not metrics:
        return None
    saw_pass = False
    for metric in metrics.values():
        if metric.verdict is False:
            return False
        if metric.verdict is True:
            saw_pass = True
    return True if saw_pass else None
