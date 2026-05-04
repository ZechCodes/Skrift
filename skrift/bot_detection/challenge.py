"""JS challenge — token signing, indicator scoring, render helper.

Every rendered HTML page that calls ``bot_detection_challenge()``
embeds a signed challenge token. The page's bundled
``challenge.js`` collects automation indicators in the browser and
posts them to ``/_bot/verify`` with the token. The server verifies
the token, applies :func:`evaluate_indicators` to derive a pass/fail
verdict, and writes the result to the cross-request store.

Token signing reuses the same HMAC-SHA256 pattern as the pixel
beacon — different namespace so a pixel token cannot be replayed at
the verify endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

from markupsafe import Markup

from skrift.bot_detection.config import JSChallengeConfig

JS_CHALLENGE_NS = "js_challenge"
TOKEN_BYTES = 12


def make_challenge_token(secret: str) -> tuple[str, str]:
    """Return ``(token, signature)`` to embed in the page."""
    token = base64.urlsafe_b64encode(
        secrets.token_bytes(TOKEN_BYTES)
    ).rstrip(b"=").decode("ascii")
    return token, _sign(secret, token)


def verify_challenge_token(secret: str, token: str, signature: str) -> bool:
    """Constant-time check of the challenge signature."""
    if not token or not signature:
        return False
    return hmac.compare_digest(_sign(secret, token), signature)


def _sign(secret: str, token: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"js-challenge-{token}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest)[:16].decode("ascii")


@dataclass(frozen=True)
class ChallengeVerdict:
    """Verdict + reason from evaluating browser indicators."""

    passed: bool
    reason: str


def evaluate_indicators(
    indicators: dict[str, Any], user_agent: str | None
) -> ChallengeVerdict:
    """Score the indicator payload reported by the browser.

    Any one of these explicit fail indicators marks the challenge as
    failed:

    - ``navigator.webdriver === true`` (Puppeteer / Playwright /
      Selenium default)
    - canvas API unavailable or threw
    - claims to be Chrome / Edge but ``window.chrome`` is missing

    Otherwise the challenge passes.
    """
    if indicators.get("webdriver") is True:
        return ChallengeVerdict(False, "navigator.webdriver = true")

    canvas = indicators.get("canvas", 0)
    if not isinstance(canvas, int) or canvas <= 0:
        return ChallengeVerdict(False, "canvas API unavailable")

    ua = (user_agent or "").lower()
    is_chromium = "chrome/" in ua or "edg/" in ua
    if is_chromium and indicators.get("chrome") is not True:
        return ChallengeVerdict(False, "Chromium UA without window.chrome")

    return ChallengeVerdict(True, "challenge passed")


def render_challenge_tag(
    token: str, signature: str, *, csp_nonce: str = ""
) -> Markup:
    """Render the ``<script>`` tag that loads challenge.js with token data.

    Caller passes the per-request CSP nonce so the script is allowed
    under the strict-script-src CSP policy.
    """
    nonce_attr = f' nonce="{csp_nonce}"' if csp_nonce else ""
    return Markup(
        f'<script src="/static/bot_detection/challenge.js"{nonce_attr} '
        f'data-token="{token}" data-signature="{signature}" defer></script>'
    )
