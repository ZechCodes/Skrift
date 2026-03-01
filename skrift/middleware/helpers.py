"""Shared helpers for ASGI middleware."""

from litestar.types import Send


async def send_not_found(send: Send) -> None:
    """Send a plain-text 404 response."""
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"Not Found"})
