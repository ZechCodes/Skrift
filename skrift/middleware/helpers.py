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


async def send_file(send: Send, content: bytes, media_type: str) -> None:
    """Send a 200 response carrying raw file bytes."""
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", media_type.encode()),
            (b"content-length", str(len(content)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": content})


async def send_redirect(send: Send, location: str, status: int = 302) -> None:
    """Send a redirect response to the given location."""
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"location", location.encode()),
            (b"content-length", b"0"),
        ],
    })
    await send({"type": "http.response.body", "body": b""})
