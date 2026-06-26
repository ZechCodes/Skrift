"""Backend-agnostic image scaling: helper + storage middleware."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from skrift.db.services.asset_service import image_url, internal_asset_url
from skrift.middleware.storage import StorageFilesMiddleware
from skrift.storage.base import StoredFile


def _png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


class FakeBackend:
    """In-memory storage backend whose URL scheme is configurable.

    When ``base_url`` is set it mimics a remote/CDN backend (absolute URLs);
    otherwise it mimics a local backend (Skrift-internal ``/storage`` URLs).
    """

    def __init__(self, store_name: str = "default", base_url: str | None = None) -> None:
        self._store_name = store_name
        self._base_url = base_url
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []

    async def put(self, key: str, data: bytes, content_type: str) -> StoredFile:
        self.objects[key] = data
        self.put_calls.append(key)
        return StoredFile(key, await self.get_url(key), content_type, len(data), "")

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def list_keys(self, prefix: str = ""):
        for key in self.objects:
            if key.startswith(prefix):
                yield key

    async def get_url(self, key: str) -> str:
        if self._base_url:
            return f"{self._base_url}/{key}"
        return internal_asset_url(self._store_name, key)


class FakeManager:
    def __init__(self, backend: FakeBackend, default: str = "default") -> None:
        self._backend = backend
        self.default_store = default

    async def get(self, name: str | None = None) -> FakeBackend:
        if name not in (None, self.default_store):
            raise KeyError(name)
        return self._backend


async def _run(middleware: StorageFilesMiddleware, path: str, query: bytes = b""):
    """Drive the middleware and capture the ASGI response messages."""
    scope = {"type": "http", "path": path, "query_string": query}
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    called = {"downstream": False}

    async def app(scope, receive, send):
        called["downstream"] = True

    middleware.app = app
    await middleware(scope, receive, send)
    return sent, called["downstream"]


def _status(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _headers(sent: list[dict]) -> dict[bytes, bytes]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"])


def _body(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


# -- image_url helper --------------------------------------------------------


async def test_image_url_without_size_returns_original_url():
    backend = FakeBackend(base_url="https://cdn.example.com")
    backend.objects["abc"] = _png_bytes(50, 50)
    manager = FakeManager(backend)

    url = await image_url(manager, "abc", store="default")

    assert url == "https://cdn.example.com/abc"


async def test_image_url_cold_path_returns_internal_size_url():
    backend = FakeBackend(base_url="https://cdn.example.com")
    backend.objects["abc"] = _png_bytes(50, 50)
    manager = FakeManager(backend)

    url = await image_url(manager, "abc", "thumb", store="default")

    # Variant does not exist yet → lazy internal URL that triggers generation.
    assert url == "/storage/default/abc?size=thumb"


async def test_image_url_warm_path_returns_direct_variant_url():
    backend = FakeBackend(base_url="https://cdn.example.com")
    backend.objects["abc"] = _png_bytes(50, 50)
    backend.objects["abc.thumb"] = _png_bytes(20, 20)
    manager = FakeManager(backend)

    url = await image_url(manager, "abc", "thumb", store="default")

    # Variant exists → direct CDN URL, no Skrift round-trip.
    assert url == "https://cdn.example.com/abc.thumb"


# -- middleware: remote backend (redirect) -----------------------------------


async def test_remote_sized_request_generates_variant_and_redirects():
    backend = FakeBackend(base_url="https://cdn.example.com")
    backend.objects["abc"] = _png_bytes(1000, 800)
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/default/abc", b"size=thumb")

    assert _status(sent) == 302
    assert _headers(sent)[b"location"] == b"https://cdn.example.com/abc.thumb"
    # Variant was generated and cached back into the backend.
    assert "abc.thumb" in backend.objects
    assert backend.put_calls == ["abc.thumb"]


async def test_remote_warm_request_does_not_regenerate():
    backend = FakeBackend(base_url="https://cdn.example.com")
    backend.objects["abc"] = _png_bytes(1000, 800)
    backend.objects["abc.thumb"] = _png_bytes(200, 160)
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/default/abc", b"size=thumb")

    assert _status(sent) == 302
    assert _headers(sent)[b"location"] == b"https://cdn.example.com/abc.thumb"
    assert backend.put_calls == []  # served the cached variant, no regeneration


# -- middleware: local backend (inline bytes) --------------------------------


async def test_local_sized_request_serves_resized_bytes_inline():
    backend = FakeBackend(base_url=None)  # internal URLs → served inline
    backend.objects["abc"] = _png_bytes(1000, 1000)
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/default/abc", b"size=thumb")

    assert _status(sent) == 200
    assert _headers(sent)[b"content-type"] == b"image/png"
    resized = Image.open(io.BytesIO(_body(sent)))
    assert resized.size == (200, 200)
    assert "abc.thumb" in backend.objects


async def test_local_original_request_serves_bytes_inline():
    backend = FakeBackend(base_url=None)
    original = _png_bytes(40, 40)
    backend.objects["abc"] = original
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/default/abc")

    assert _status(sent) == 200
    assert _body(sent) == original


# -- middleware: edge cases --------------------------------------------------


async def test_non_image_with_size_falls_back_to_original():
    backend = FakeBackend(base_url=None)
    backend.objects["doc"] = b"%PDF-1.4 not an image"
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/default/doc", b"size=thumb")

    assert _status(sent) == 200
    assert _body(sent) == b"%PDF-1.4 not an image"
    assert backend.put_calls == []  # never tried to resize a non-image


async def test_missing_original_returns_404():
    backend = FakeBackend(base_url=None)
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/default/missing")

    assert _status(sent) == 404


async def test_unknown_store_returns_404():
    backend = FakeBackend(base_url=None)
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/other/abc")

    assert _status(sent) == 404


async def test_path_traversal_is_rejected():
    backend = FakeBackend(base_url=None)
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, _ = await _run(middleware, "/storage/default/../secret")

    assert _status(sent) == 404


async def test_non_storage_path_passes_through():
    backend = FakeBackend(base_url=None)
    middleware = StorageFilesMiddleware(app=None, storage_manager=FakeManager(backend))

    sent, downstream = await _run(middleware, "/about")

    assert downstream is True
    assert sent == []
