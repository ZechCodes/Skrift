"""Safe-redirect helpers used by auth flows.

Kept in ``skrift.lib`` so that auth methods can import them without
pulling in the controller layer (which would create an import cycle).
"""

from __future__ import annotations

import fnmatch
from urllib.parse import urlparse

from litestar import Request

from skrift.auth.session_keys import SESSION_AUTH_NEXT


def is_safe_redirect_url(url: str, allowed_domains: list[str]) -> bool:
    """Check if URL is safe to redirect to.

    Supports wildcard patterns using fnmatch-style matching:
    - "*.example.com" matches any subdomain of example.com
    - "app-*.example.com" matches app-foo.example.com, app-bar.example.com, etc.
    - "example.com" (no wildcards) matches example.com and all subdomains
    """
    if url.startswith("/") and not url.startswith("//"):
        return True

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if not parsed.scheme or not parsed.netloc:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.netloc.lower().split(":")[0]
    for pattern in allowed_domains:
        pattern = pattern.lower()
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(host, pattern):
                return True
        else:
            if host == pattern or host.endswith(f".{pattern}"):
                return True

    return False


def get_safe_redirect_url(request: Request, allowed_domains: list[str], default: str = "/") -> str:
    """Get the next redirect URL from session, validating it's safe."""
    next_url = request.session.pop(SESSION_AUTH_NEXT, None)
    if next_url and is_safe_redirect_url(next_url, allowed_domains):
        return next_url
    return default
