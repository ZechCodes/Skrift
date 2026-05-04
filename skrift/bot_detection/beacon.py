"""Pixel beacon helpers — token signing and HTML snippet rendering.

The pixel URL embeds a short signed token so an attacker cannot
hand-craft pixel hits for arbitrary IPs without having seen a
genuine HTML response. Beyond that, the token is mostly cache-busting
— each rendered page gets a fresh URL so CDN caches do not hide the
miss when the pixel never loads.

State lives in the cross-request store under
:data:`PIXEL_LOADED_NS` keyed by client IP. A successful pixel hit
sets the key with TTL :attr:`PixelBeaconConfig.cache_ttl`; the metric
reads it on subsequent requests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from markupsafe import Markup

PIXEL_LOADED_NS = "pixel_loaded"
TOKEN_BYTES = 12  # ~16 base64-url chars


def make_pixel_token(secret: str) -> tuple[str, str]:
    """Return ``(token, signature)`` — the parts go in the pixel URL.

    The token is random, the signature is HMAC-SHA256(secret, token)
    truncated. Both halves are url-safe base64 without padding.
    """
    token = base64.urlsafe_b64encode(
        secrets.token_bytes(TOKEN_BYTES)
    ).rstrip(b"=").decode("ascii")
    signature = _sign(secret, token)
    return token, signature


def verify_pixel_token(secret: str, token: str, signature: str) -> bool:
    """Constant-time check that ``signature`` matches ``token``."""
    if not token or not signature:
        return False
    expected = _sign(secret, token)
    return hmac.compare_digest(expected, signature)


def _sign(secret: str, token: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest)[:16].decode("ascii")


def render_pixel_tag(
    token: str, signature: str, *, css_beacon: bool = False
) -> Markup:
    """Render the HTML for the pixel beacon, optionally with a CSS one too.

    Output is marked safe — callers embed via ``{{ bot_detection_pixel() }}``
    in their base template.
    """
    pixel = (
        f'<img src="/_bot/p.gif?t={token}&s={signature}" '
        'width="1" height="1" alt="" aria-hidden="true" '
        'style="position:absolute;left:-9999px;width:1px;height:1px;">'
    )
    if not css_beacon:
        return Markup(pixel)
    css = (
        f'<span aria-hidden="true" '
        f'style="background-image:url(/_bot/c.gif?t={token}&s={signature});'
        'position:absolute;left:-9999px;width:1px;height:1px;"></span>'
    )
    return Markup(pixel + css)
