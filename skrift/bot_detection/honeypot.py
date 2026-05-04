"""Robots.txt honeypot — rotating-token trap for non-compliant scrapers.

Strategy:

1. ``Disallow: <trap_path>/<token>`` is appended to ``robots.txt`` via
   the :data:`~skrift.lib.hooks.ROBOTS_TXT` filter. The token rotates
   weekly so cached crawler maps go stale.
2. Every fetch of ``robots.txt`` records the client IP via the
   :data:`~skrift.lib.hooks.ROBOTS_TXT_FETCHED` action. The metric
   later reads this state to distinguish "read the rules and ignored
   them" (strongest signal) from "did not even read robots.txt" (still
   suspicious but lower confidence).
3. A request that hits any path under ``trap_path/`` is recorded as a
   trap hit. The :data:`~skrift.bot_detection.hooks.BOT_TRAP_HIT`
   action fires so audit code can react. The middleware itself returns
   a 404 to make the trap look like a normal block.

Token rotation uses HMAC-SHA256(secret, "trap-{period}") truncated to
16 base64 url-safe characters. The secret is the application's
``settings.secret_key`` so it is unique per deployment. The period is
``floor(epoch_seconds / (rotate_token_days * 86400))``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from skrift.bot_detection.config import RobotsHoneypotConfig

ROBOTS_READ_NS = "robots_read"
TRAP_HIT_NS = "trap_hit"

# Cross-request state lives in the store with a TTL well past the
# rotation period so the metric still has signal a few weeks after
# robots.txt was fetched.
STATE_TTL_SECONDS = 30 * 86400


def make_trap_token(secret: str, rotate_token_days: int) -> str:
    """Generate the current trap token for ``secret`` and rotation period."""
    period_seconds = max(1, rotate_token_days) * 86400
    period = int(time.time() // period_seconds)
    digest = hmac.new(
        secret.encode("utf-8"),
        f"bot-detection-trap-{period}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest)[:16].decode("ascii")


def trap_url(config: RobotsHoneypotConfig, secret: str) -> str:
    """Full URL path of the trap, with the current rotating token."""
    token = make_trap_token(secret, config.rotate_token_days)
    return f"{config.trap_path.rstrip('/')}/{token}"


def is_trap_path(path: str, config: RobotsHoneypotConfig) -> bool:
    """Return True when ``path`` lies under the configured trap prefix."""
    prefix = config.trap_path.rstrip("/")
    if not prefix:
        return False
    return path == prefix or path.startswith(prefix + "/")


def inject_disallow_rule(content: str, trap_url_value: str) -> str:
    """Append a ``Disallow:`` rule to robots.txt content.

    Appends to the first ``User-agent: *`` block when present;
    otherwise prepends a new block. Idempotent: if the rule already
    exists, returns the content unchanged.
    """
    rule = f"Disallow: {trap_url_value}"
    if rule in content:
        return content

    lines = content.splitlines()
    user_agent_idx = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "user-agent: *":
            user_agent_idx = i
            break

    if user_agent_idx is None:
        new_block = f"User-agent: *\n{rule}\n\n"
        return new_block + content

    # Insert the rule directly after the User-agent: * line.
    lines.insert(user_agent_idx + 1, rule)
    trailing_newline = "\n" if content.endswith("\n") else ""
    return "\n".join(lines) + trailing_newline
