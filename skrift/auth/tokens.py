"""Signed token utilities for the OAuth2 Authorization Server.

Uses HMAC-SHA256 signing with Python stdlib. No external dependencies.
"""

import base64
import hashlib
import hmac
import json
import time


def create_signed_token(payload: dict, secret: str, expires_in: int) -> str:
    """Create a base64url-encoded, HMAC-signed JSON payload with expiration.

    Args:
        payload: Dictionary to encode in the token.
        secret: HMAC signing secret.
        expires_in: Token lifetime in seconds.

    Returns:
        URL-safe base64 string: ``base64(json_payload).base64(signature)``
    """
    payload = {**payload, "exp": int(time.time()) + expires_in}
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode()

    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode()

    return f"{payload_b64}.{sig_b64}"


def verify_signed_token(token: str, secret: str) -> dict | None:
    """Verify and decode a signed token.

    Returns:
        Decoded payload dict, or ``None`` if the token is invalid, expired,
        or has been tampered with.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, sig_b64 = parts

    # Verify signature
    expected_sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    try:
        actual_sig = base64.urlsafe_b64decode(sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    # Decode payload
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes)
    except Exception:
        return None

    # Check expiration
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or time.time() > exp:
        return None

    return payload
