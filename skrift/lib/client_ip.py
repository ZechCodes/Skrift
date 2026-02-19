"""Client IP extraction from ASGI scope."""

from litestar.types import Scope


def get_client_ip(scope: Scope) -> str:
    """Extract client IP, checking x-forwarded-for first."""
    headers = dict(scope.get("headers", []))
    forwarded = headers.get(b"x-forwarded-for")
    if forwarded:
        return forwarded.decode().split(",")[0].strip()
    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"
