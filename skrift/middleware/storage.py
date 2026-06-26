"""ASGI middleware for serving stored assets across any storage backend."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.lib.imaging import IMAGE_SIZES, detect_image_content_type, resize_image, variant_filename
from skrift.middleware.helpers import send_file, send_not_found, send_redirect

if TYPE_CHECKING:
    from skrift.storage.base import StorageBackend
    from skrift.storage.manager import StorageManager

logger = logging.getLogger(__name__)


class StorageFilesMiddleware:
    """Serve stored assets at ``/storage/{store}/{key}`` through any backend.

    Resolves the request through the configured :class:`StorageManager`, so it
    works for local, S3, and custom backends alike. Supports ``?size=name`` for
    on-demand image resizing: the variant is generated once, cached in the same
    backend under a ``{key}.{size}`` key, then served inline (for backends with
    Skrift-internal URLs) or redirected to (for backends with external/CDN URLs)
    so subsequent warm requests bypass Skrift entirely.
    """

    def __init__(self, app: ASGIApp, storage_manager: StorageManager) -> None:
        self.app = app
        self._storage = storage_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/storage/"):
            await self.app(scope, receive, send)
            return

        # Parse /storage/{store_name}/{key...}
        rest = scope["path"][len("/storage/"):]
        slash_idx = rest.find("/")
        if slash_idx <= 0:
            await send_not_found(send)
            return

        store_name = rest[:slash_idx]
        key = rest[slash_idx + 1:]

        # Security: reject empty keys, traversal, and null bytes
        if not key or ".." in key.split("/") or "\x00" in key:
            await send_not_found(send)
            return

        try:
            backend = await self._storage.get(store_name)
        except KeyError:
            await send_not_found(send)
            return

        size_name = self._requested_size(scope)
        serve_key = key
        if size_name in IMAGE_SIZES:
            variant_key = await self._ensure_variant(backend, key, size_name)
            if variant_key is not None:
                serve_key = variant_key

        target_url = await backend.get_url(serve_key)
        if target_url.startswith(("http://", "https://")):
            await send_redirect(send, target_url)
            return

        if not await backend.exists(serve_key):
            await send_not_found(send)
            return

        content = await backend.get(serve_key)
        media_type = (
            detect_image_content_type(content)
            or mimetypes.guess_type(serve_key)[0]
            or "application/octet-stream"
        )
        await send_file(send, content, media_type)

    @staticmethod
    def _requested_size(scope: Scope) -> str | None:
        qs = scope.get("query_string", b"")
        params = parse_qs(qs.decode("latin-1") if isinstance(qs, bytes) else qs)
        return params.get("size", [None])[0]

    @staticmethod
    async def _ensure_variant(
        backend: StorageBackend, key: str, size_name: str
    ) -> str | None:
        """Return the variant key, generating and caching it on first request.

        Returns ``None`` when the original is missing or is not a recognized
        image, signalling the caller to fall back to the original key.
        """
        variant_key = variant_filename(key, size_name)
        if await backend.exists(variant_key):
            return variant_key

        if not await backend.exists(key):
            return None

        original = await backend.get(key)
        if detect_image_content_type(original) is None:
            return None

        max_width, max_height = IMAGE_SIZES[size_name]
        resized, content_type = await asyncio.to_thread(
            resize_image, original, max_width, max_height
        )
        await backend.put(variant_key, resized, content_type)
        return variant_key
