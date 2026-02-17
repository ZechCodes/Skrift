import mimetypes
from pathlib import Path

from litestar.types import ASGIApp, Receive, Scope, Send


class StaticFilesMiddleware:
    """ASGI middleware that serves static files before Litestar's router.

    Intercepts ``/static/*`` requests and resolves files from a mutable list
    of directories. This avoids route conflicts with catch-all page handlers
    and ensures non-HTML 404s are returned as plain text (not JSON).
    """

    def __init__(self, app: ASGIApp, directories: list[Path]) -> None:
        self.app = app
        self.directories = directories  # mutable reference — theme switches take effect immediately

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            await self.app(scope, receive, send)
            return

        filepath = scope["path"][len("/static/"):]
        resolved = self._find_file(filepath)

        if resolved is None:
            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({"type": "http.response.body", "body": b"Not Found"})
            return

        media_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        content = resolved.read_bytes()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", media_type.encode()),
                (b"content-length", str(len(content)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": content})

    def _find_file(self, filepath: str) -> Path | None:
        for directory in self.directories:
            full = directory / filepath
            if full.is_file():
                return full
        return None
