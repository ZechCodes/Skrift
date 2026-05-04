"""Base metric protocol and small helpers.

Each metric is a callable that inspects the ASGI scope (and, for
deferred metrics, prior state stored in :class:`BotStateStore`) and
returns a :class:`MetricResult`. The middleware runs every enabled
metric per request and assembles their results into a
:class:`BotDetectionResult`.

End users — and the existing built-ins — implement metrics by
subclassing :class:`BotMetric` (or any class that satisfies the
protocol) and registering them through the ``bot_metrics`` filter or
by listing them in code.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from litestar.types import Scope

from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import MetricResult


@runtime_checkable
class BotMetric(Protocol):
    """Protocol every bot-detection metric must satisfy.

    Stateless metrics (UA / header inspection) ignore ``store``.
    Stateful metrics (pixel, JS challenge, honeypot) read prior
    cross-request state through it.
    """

    name: ClassVar[str]
    enabled: bool

    async def check(self, scope: Scope, store: BotStateStore) -> MetricResult:
        ...


def get_header(scope: Scope, name: str) -> str | None:
    """Return the first matching header value as a decoded string.

    Header names are matched case-insensitively. Returns ``None`` if
    the header is not present or cannot be decoded.
    """
    target = name.lower().encode()
    for key, value in scope.get("headers", ()):
        if key.lower() == target:
            try:
                return value.decode("latin-1")
            except (UnicodeDecodeError, AttributeError):
                return None
    return None
