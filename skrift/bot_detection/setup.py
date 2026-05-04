"""Hook handler registration for the bot detection component.

Called once at app startup. Registers:

- A :data:`~skrift.lib.hooks.ROBOTS_TXT` filter that injects the
  rotating ``Disallow: <trap_path>/<token>`` rule.
- A :data:`~skrift.lib.hooks.ROBOTS_TXT_FETCHED` action handler that
  records the fetcher's IP in the cross-request state store.

These are external to the middleware because they fire from the
:class:`SitemapController` request flow, not from the bot detection
middleware itself.
"""

from __future__ import annotations

import logging

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.honeypot import (
    ROBOTS_READ_NS,
    STATE_TTL_SECONDS,
    inject_disallow_rule,
    trap_url,
)
from skrift.bot_detection.store import BotStateStore
from skrift.lib.hooks import ROBOTS_TXT, ROBOTS_TXT_FETCHED, hooks

logger = logging.getLogger(__name__)


def setup_honeypot_hooks(
    config: BotDetectionConfig,
    store: BotStateStore,
    secret: str,
) -> None:
    """Register the ROBOTS_TXT filter + ROBOTS_TXT_FETCHED action.

    Idempotent — safe to call once per app construction. The
    ``secret`` is used to sign the rotating trap token; deployments
    should pass ``settings.secret_key``.
    """
    if not (config.enabled and config.robots_honeypot.enabled):
        return

    honeypot_config = config.robots_honeypot

    async def _inject_trap_rule(content: str) -> str:
        try:
            url = trap_url(honeypot_config, secret)
        except Exception:
            logger.warning("bot_detection trap_url generation failed", exc_info=True)
            return content
        return inject_disallow_rule(content, url)

    async def _record_robots_fetch(request, ip: str, _ua: str) -> None:
        if not honeypot_config.log_robots_fetches:
            return
        try:
            await store.set(ROBOTS_READ_NS, ip, "1", ttl=STATE_TTL_SECONDS)
        except Exception:
            logger.warning(
                "bot_detection robots_read store update failed", exc_info=True
            )

    hooks.add_filter(ROBOTS_TXT, _inject_trap_rule)
    hooks.add_action(ROBOTS_TXT_FETCHED, _record_robots_fetch)
