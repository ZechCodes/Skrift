"""Builders that wire the bot detection middleware into Litestar.

These helpers are called once during app startup
(``skrift/asgi.py``) when ``settings.bot_detection.enabled`` is
``True``. They construct the cross-request state store, instantiate
the built-in metrics, run the
:data:`~skrift.bot_detection.hooks.BOT_METRICS` startup filter so
plugins can inject custom metrics, and produce the resolved metric
list passed into :class:`BotDetectionMiddleware`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.hooks import BOT_METRICS
from skrift.bot_detection.metrics import BUILTIN_METRICS
from skrift.bot_detection.metrics.base import BotMetric
from skrift.bot_detection.store import (
    BotStateStore,
    InMemoryBotStateStore,
    RedisBotStateStore,
)
from skrift.lib.hooks import hooks

if TYPE_CHECKING:
    from skrift.config import RedisConfig

logger = logging.getLogger(__name__)


def build_bot_state_store(
    config: BotDetectionConfig,
    redis_config: RedisConfig,
    redis_client=None,
) -> BotStateStore:
    """Construct the cross-request state store.

    Returns a :class:`RedisBotStateStore` when ``cache_backend`` is
    ``"redis"`` and a Redis client is available, otherwise falls back
    to :class:`InMemoryBotStateStore`.
    """
    if config.cache_backend == "redis" and redis_client is not None:
        prefix = redis_config.make_key("skrift", "bot_detection")
        return RedisBotStateStore(redis_client, prefix=prefix)
    if config.cache_backend == "redis" and redis_client is None:
        logger.warning(
            "bot_detection cache_backend=redis but no Redis client; "
            "falling back to in-memory store"
        )
    return InMemoryBotStateStore()


def build_initial_metrics(
    config: BotDetectionConfig,
) -> list[BotMetric]:
    """Instantiate the built-in metrics that are enabled by config.

    Synchronous so it can be called during app construction. The
    :data:`~skrift.bot_detection.hooks.BOT_METRICS` filter is applied
    later in :func:`apply_metrics_filter` (during ``on_startup``) when
    plugin filter handlers are guaranteed to be registered.
    """
    metrics: list[BotMetric] = []
    for metric_cls in BUILTIN_METRICS:
        instance = metric_cls(config)  # type: ignore[call-arg]
        if instance.enabled:
            metrics.append(instance)
    return metrics


async def apply_metrics_filter(
    metrics: list[BotMetric],
    config: BotDetectionConfig,
) -> None:
    """Run the :data:`BOT_METRICS` filter and update ``metrics`` in place.

    Called from ``on_startup`` after Litestar has finished importing
    user controllers (which may register filter handlers via the
    ``@filter`` decorator). Mutates the list passed to the middleware
    so the new entries take effect on the next request.
    """
    filtered = await hooks.apply_filters(BOT_METRICS, list(metrics), config)
    metrics.clear()
    metrics.extend(filtered)
