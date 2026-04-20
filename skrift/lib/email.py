"""Email backend DI helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from litestar import Request

    from skrift.lib.email_backends import EmailBackend


def get_email_backend(request: "Request") -> "EmailBackend":
    """Return the configured email backend attached to the app at startup."""
    return request.app.state.email_backend
