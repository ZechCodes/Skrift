"""ASGI middleware for serving locally-stored assets."""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.lib.imaging import IMAGE_SIZES, detect_image_content_type, resize_image, variant_filename
from skrift.middleware.helpers import send_not_found

if TYPE_CHECKING:
    from skrift.config import StorageConfig

logger = logging.getLogger(__name__)


class StorageFilesMiddleware:
    """Serve files from local storage backends at ``/storage/{store}/{key}``.

    Only handles stores configured with ``backend = "local"``.  Remote backends
    (S3, etc.) serve via their own URLs and are not intercepted here.

    Supports ``?size=name`` query parameter for on-demand image resizing.
    Resized variants are cached alongside originals for subsequent requests.
    """

    def __init__(self, app: ASGIApp, storage_config: StorageConfig) -> None:
        self.app = app
        self._storage_config = storage_config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/storage/"):
            await self.app(scope, receive, send)
            return

        # Parse /storage/{store_name}/{key...}
        rest = scope["path"][len("/storage/"):]
        slash_idx = rest.find("/")
        if slash_idx == -1 or not rest[:slash_idx]:
            await send_not_found(send)
            return

        store_name = rest[:slash_idx]
        key = rest[slash_idx + 1:]

        if not key:
            await send_not_found(send)
            return

        # Only serve local backends
        store_cfg = self._storage_config.stores.get(store_name)
        if store_cfg is None or store_cfg.backend != "local":
            await send_not_found(send)
            return

        # Security: reject traversal and null bytes
        if ".." in key.split("/") or "\x00" in key:
            await send_not_found(send)
            return

        base_path = Path(store_cfg.local_path).resolve()

        # Reconstruct the on-disk path through LocalStorageBackend's layout
        if len(key) >= 4:
            candidate = base_path / key[:2] / key[2:4] / key
        else:
            candidate = base_path / key

        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            await send_not_found(send)
            return

        if not resolved.is_relative_to(base_path):
            await send_not_found(send)
            return

        if not resolved.is_file():
            await send_not_found(send)
            return

        # Check for ?size=name variant request
        qs = scope.get("query_string", b"")
        params = parse_qs(qs.decode("latin-1") if isinstance(qs, bytes) else qs)
        size_name = params.get("size", [None])[0]

        result = None
        if size_name and size_name in IMAGE_SIZES:
            result = self._get_or_create_variant(resolved, size_name)

        if result is not None:
            content, media_type = result
        else:
            content = resolved.read_bytes()
            media_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", media_type.encode()),
                (b"content-length", str(len(content)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": content})

    @staticmethod
    def _get_or_create_variant(original: Path, size_name: str) -> tuple[bytes, str] | None:
        """Return cached variant bytes or generate and cache them.

        The variant file is stored alongside the original with a ``.{size_name}``
        suffix (e.g. ``abc123def456.thumb``).

        Returns ``None`` if the original is not a recognized image format.
        """
        variant_name = variant_filename(original.name, size_name)
        variant_path = original.parent / variant_name

        if variant_path.is_file():
            content = variant_path.read_bytes()
            ct = detect_image_content_type(content) or "application/octet-stream"
            return content, ct

        original_bytes = original.read_bytes()

        # Skip non-image files
        if detect_image_content_type(original_bytes) is None:
            return None

        max_w, max_h = IMAGE_SIZES[size_name]
        resized_bytes, content_type = resize_image(original_bytes, max_w, max_h)

        try:
            variant_path.write_bytes(resized_bytes)
        except OSError:
            logger.warning("Failed to cache variant %s", variant_path, exc_info=True)

        return resized_bytes, content_type
