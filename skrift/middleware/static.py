import mimetypes
from pathlib import Path

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.middleware.helpers import send_not_found


def resolve_static_file(
    source: str,
    filepath: str,
    themes_dir: Path,
    site_static_dir: Path,
    package_static_dir: Path,
) -> Path | None:
    """Resolve a static file path from a source namespace and relative filepath.

    Used by both StaticFilesMiddleware and StaticHasher to avoid duplication.

    Returns the resolved Path if the file exists, or None if not found.
    """
    # Reject path traversal in source or filepath
    if ".." in source.split("/") or ".." in filepath.split("/"):
        return None
    if "\x00" in source or "\x00" in filepath:
        return None

    if source == "skrift":
        root = package_static_dir
        candidate = root / filepath
    elif source == "site":
        root = site_static_dir
        candidate = root / filepath
    else:
        root = themes_dir / source / "static"
        candidate = root / filepath

    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None

    if not resolved.is_relative_to(root.resolve()):
        return None

    if resolved.is_file():
        return resolved

    return None


class StaticFilesMiddleware:
    """ASGI middleware that serves static files before Litestar's router.

    Intercepts ``/static/{source}/*`` requests and resolves files from a
    fixed set of directories based on the source namespace:

    - ``/static/skrift/...`` — package assets (skrift/static/)
    - ``/static/site/...`` — site-level assets (./static/)
    - ``/static/{theme}/...`` — theme assets (./themes/{theme}/static/)

    This avoids route conflicts with catch-all page handlers and ensures
    non-HTML 404s are returned as plain text (not JSON).
    """

    def __init__(
        self,
        app: ASGIApp,
        themes_dir: Path,
        site_static_dir: Path,
        package_static_dir: Path,
    ) -> None:
        self.app = app
        self.themes_dir = themes_dir
        self.site_static_dir = site_static_dir
        self.package_static_dir = package_static_dir

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            await self.app(scope, receive, send)
            return

        # Strip "/static/" prefix, split into source + filepath
        rest = scope["path"][len("/static/"):]
        slash_idx = rest.find("/")
        if slash_idx == -1 or not rest[:slash_idx]:
            await send_not_found(send)
            return

        source = rest[:slash_idx]
        filepath = rest[slash_idx + 1:]

        if not filepath:
            await send_not_found(send)
            return

        resolved = resolve_static_file(
            source, filepath, self.themes_dir, self.site_static_dir, self.package_static_dir
        )

        if resolved is None:
            await send_not_found(send)
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
