"""Client IP extraction from ASGI scope.

Reads from ``scope["state"]["client_ip"]`` when
:class:`~skrift.middleware.client_ip.ClientIPMiddleware` has already run
(the normal path). Falls back to the socket peer when state is missing,
which only happens in tests or pre-middleware code paths. The legacy
behavior of naively honoring ``X-Forwarded-For`` is intentionally gone —
that was the spoofable path tracked by issue #120.
"""

from litestar.types import Scope


def get_client_ip(scope: Scope) -> str:
    """Return the resolved client IP for an ASGI scope."""
    state = scope.get("state")
    if isinstance(state, dict):
        resolved = state.get("client_ip")
        if isinstance(resolved, str) and resolved:
            return resolved

    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"


def get_client_ip_source(scope: Scope) -> str:
    """Return the provenance tag for the resolved client IP.

    One of ``"socket"``, ``"xff"``, ``"xff-all-trusted"``, a CDN header
    name (lowercase), or ``"unknown"`` when middleware hasn't run.
    """
    state = scope.get("state")
    if isinstance(state, dict):
        source = state.get("client_ip_source")
        if isinstance(source, str) and source:
            return source
    return "unknown"
