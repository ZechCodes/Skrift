"""Tests for Skrift's bounded gzip compression middleware.

Covers the three memory-bounding behaviours:

* at most ``max_concurrent_compressions`` zlib deflate states are alive at once,
  and responses past the cap are sent uncompressed rather than queued,
* compressed bytes are only flushed when a streaming chunk must reach the
  client (buffered responses accumulate and flush once, at close),
* ``compression.enabled = false`` removes compression entirely.
"""

import gzip
from io import BytesIO

import pytest
from litestar import Litestar, get
from litestar.response import Stream
from litestar.testing import TestClient

from litestar.middleware import DefineMiddleware

import skrift.config as config_mod
from skrift.config import CompressionConfig
from skrift.middleware.compression import (
    DEFAULT_MAX_CONCURRENT_COMPRESSIONS,
    STREAMING_EXCLUDE_PATTERN,
    BoundedCompressionConfig,
    BoundedCompressionMiddleware,
    ConcurrentCompressionLimiter,
    SafeGzipCompression,
    build_compression_config,
    build_compression_middleware,
    process_compression_limiter,
)

LARGE_BODY = "compress me. " * 400
SMALL_BODY = "tiny"


def _config(**overrides) -> BoundedCompressionConfig:
    """A gzip config wired to an isolated limiter so tests never share slots."""
    settings = {
        "backend": "gzip",
        "compression_facade": SafeGzipCompression,
        "middleware_class": BoundedCompressionMiddleware,
        "minimum_size": 100,
        "compression_limiter": ConcurrentCompressionLimiter(),
    }
    settings.update(overrides)
    return BoundedCompressionConfig(**settings)


def _build_app(*compression_middleware) -> Litestar:
    """Build an app wired the way ``skrift.asgi`` wires compression."""
    @get("/large", media_type="text/plain", sync_to_thread=False)
    def large() -> str:
        return LARGE_BODY

    @get("/small", media_type="text/plain", sync_to_thread=False)
    def small() -> str:
        return SMALL_BODY

    @get("/notifications/stream", media_type="text/event-stream", sync_to_thread=False)
    def stream() -> Stream:
        def chunks():
            for index in range(5):
                yield f"data: {LARGE_BODY}{index}\n\n".encode()

        return Stream(chunks)

    @get("/stream", media_type="text/plain", sync_to_thread=False)
    def plain_stream() -> Stream:
        def chunks():
            for index in range(5):
                yield f"chunk-{index}-{LARGE_BODY}".encode()

        return Stream(chunks)

    return Litestar(
        route_handlers=[large, small, stream, plain_stream],
        middleware=list(compression_middleware),
        openapi_config=None,
    )


def _app_for(config: BoundedCompressionConfig) -> Litestar:
    return _build_app(DefineMiddleware(BoundedCompressionMiddleware, config=config))


class TestConcurrentCompressionLimiter:
    def test_try_acquire_bounded_by_limit(self):
        limiter = ConcurrentCompressionLimiter()

        assert limiter.try_acquire(2) is True
        assert limiter.try_acquire(2) is True
        assert limiter.try_acquire(2) is False
        assert limiter.active_compressors == 2

    def test_release_frees_a_slot(self):
        limiter = ConcurrentCompressionLimiter()
        limiter.try_acquire(1)

        limiter.release()

        assert limiter.active_compressors == 0
        assert limiter.try_acquire(1) is True

    def test_release_without_acquire_fails_fast(self):
        limiter = ConcurrentCompressionLimiter()

        with pytest.raises(RuntimeError):
            limiter.release()

    def test_default_cap_is_small(self):
        assert DEFAULT_MAX_CONCURRENT_COMPRESSIONS == 20


class TestSafeGzipCompressionSlots:
    def test_acquire_returns_none_past_the_cap(self):
        config = _config(max_concurrent_compressions=3)
        facades = [
            SafeGzipCompression.acquire(BytesIO(), "gzip", config) for _ in range(3)
        ]

        assert all(facade is not None for facade in facades)
        assert SafeGzipCompression.acquire(BytesIO(), "gzip", config) is None

        facades[0].close()
        replacement = SafeGzipCompression.acquire(BytesIO(), "gzip", config)
        assert replacement is not None

        for facade in [*facades[1:], replacement]:
            facade.close()

    def test_close_releases_the_slot_once(self):
        config = _config(max_concurrent_compressions=1)
        facade = SafeGzipCompression.acquire(BytesIO(), "gzip", config)

        facade.close()
        facade.close()

        assert config.compression_limiter.active_compressors == 0

    def test_close_survives_a_closed_buffer(self):
        """Python 3.13's GzipFile finalizer raises on an already-closed buffer."""
        config = _config(max_concurrent_compressions=1)
        buffer = BytesIO()
        facade = SafeGzipCompression.acquire(buffer, "gzip", config)
        facade.write(b"x" * 100)
        buffer.close()

        facade.close()

        assert config.compression_limiter.active_compressors == 0

    def test_direct_construction_holds_no_slot(self):
        config = _config(max_concurrent_compressions=1)

        facade = SafeGzipCompression(BytesIO(), "gzip", config)
        facade.close()

        assert config.compression_limiter.active_compressors == 0


class TestFacadeFlushing:
    def test_write_does_not_flush(self):
        config = _config()
        buffer = BytesIO()
        facade = SafeGzipCompression(buffer, "gzip", config)
        header_size = len(buffer.getvalue())

        facade.write(b"a" * 5_000)

        assert len(buffer.getvalue()) == header_size
        facade.close()

    def test_flush_emits_pending_bytes(self):
        config = _config()
        buffer = BytesIO()
        facade = SafeGzipCompression(buffer, "gzip", config)
        facade.write(b"a" * 5_000)

        facade.flush()

        assert len(buffer.getvalue()) > 0
        facade.close()

    def test_close_produces_a_valid_gzip_stream(self):
        config = _config()
        buffer = BytesIO()
        facade = SafeGzipCompression(buffer, "gzip", config)
        facade.write(b"hello ")
        facade.write(b"world")

        facade.close()

        assert gzip.decompress(buffer.getvalue()) == b"hello world"


class TestCompressionMiddlewareResponses:
    def test_response_over_minimum_size_is_gzipped(self):
        config = _config()
        with TestClient(app=_app_for(config)) as client:
            response = client.get("/large", headers={"accept-encoding": "gzip"})

        assert response.headers["content-encoding"] == "gzip"
        assert response.text == LARGE_BODY

    def test_response_under_minimum_size_is_not_compressed(self):
        config = _config(minimum_size=2048)
        with TestClient(app=_app_for(config)) as client:
            response = client.get("/small", headers={"accept-encoding": "gzip"})

        assert "content-encoding" not in response.headers
        assert response.text == SMALL_BODY

    def test_no_slot_falls_back_to_identity(self):
        config = _config(max_concurrent_compressions=1)
        held = SafeGzipCompression.acquire(BytesIO(), "gzip", config)
        assert held is not None

        with TestClient(app=_app_for(config)) as client:
            response = client.get("/large", headers={"accept-encoding": "gzip"})
        held.close()

        assert "content-encoding" not in response.headers
        assert response.text == LARGE_BODY

    def test_slot_is_released_after_a_compressed_response(self):
        config = _config()
        with TestClient(app=_app_for(config)) as client:
            for _ in range(5):
                client.get("/large", headers={"accept-encoding": "gzip"})

        assert config.compression_limiter.active_compressors == 0

    def test_streaming_response_arrives_intact(self):
        config = _config()
        expected = "".join(f"chunk-{index}-{LARGE_BODY}" for index in range(5))

        with TestClient(app=_app_for(config)) as client:
            response = client.get("/stream", headers={"accept-encoding": "gzip"})

        assert response.headers["content-encoding"] == "gzip"
        assert response.text == expected
        assert config.compression_limiter.active_compressors == 0

    def test_excluded_stream_path_is_never_compressed(self):
        """Every app (primary, site, setup) excludes the SSE endpoint."""
        middleware = build_compression_middleware(CompressionConfig(minimum_size=100))

        with TestClient(app=_build_app(*middleware)) as client:
            excluded = client.get(
                "/notifications/stream", headers={"accept-encoding": "gzip"}
            )
            compressed = client.get("/large", headers={"accept-encoding": "gzip"})

        assert "content-encoding" not in excluded.headers
        assert compressed.headers["content-encoding"] == "gzip"
        assert process_compression_limiter.active_compressors == 0

    def test_disabled_compression_installs_no_middleware(self):
        middleware = build_compression_middleware(CompressionConfig(enabled=False))

        assert middleware == []
        with TestClient(app=_build_app(*middleware)) as client:
            response = client.get("/large", headers={"accept-encoding": "gzip"})

        assert "content-encoding" not in response.headers
        assert response.text == LARGE_BODY

    def test_client_without_gzip_support_gets_identity(self):
        config = _config()
        with TestClient(app=_app_for(config)) as client:
            response = client.get("/large", headers={"accept-encoding": "identity"})

        assert "content-encoding" not in response.headers
        assert response.text == LARGE_BODY
        assert config.compression_limiter.active_compressors == 0


class TestStreamingFlushBehaviour:
    """Direct ASGI drive: every streamed chunk must reach the client immediately."""

    async def _run(self, config, chunks: list[bytes]) -> list[dict]:
        async def app(scope, receive, send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            for index, chunk in enumerate(chunks):
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": index < len(chunks) - 1,
                    }
                )

        middleware = BoundedCompressionMiddleware(app=app, config=config)
        sent: list[dict] = []

        async def send(message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/stream",
            "headers": [(b"accept-encoding", b"gzip")],
            "state": {},
        }
        await middleware(scope, None, send)
        return sent

    @pytest.mark.asyncio
    async def test_each_chunk_is_flushed_and_stream_decompresses(self):
        config = _config()
        chunks = [f"event-{index}\n\n".encode() for index in range(4)]

        sent = await self._run(config, chunks)

        bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
        assert len(bodies) == len(chunks)
        assert all(body for body in bodies), "a streamed chunk produced no bytes"
        assert gzip.decompress(b"".join(bodies)) == b"".join(chunks)
        assert config.compression_limiter.active_compressors == 0

    @pytest.mark.asyncio
    async def test_streaming_without_a_slot_passes_bytes_through(self):
        config = _config(max_concurrent_compressions=1)
        held = SafeGzipCompression.acquire(BytesIO(), "gzip", config)
        chunks = [f"event-{index}\n\n".encode() for index in range(3)]

        sent = await self._run(config, chunks)
        held.close()

        start = next(m for m in sent if m["type"] == "http.response.start")
        header_names = [name.lower() for name, _ in start["headers"]]
        assert b"content-encoding" not in header_names
        bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
        assert b"".join(bodies) == b"".join(chunks)

    @pytest.mark.asyncio
    async def test_slot_released_when_the_app_raises(self):
        config = _config()

        async def failing_app(scope, receive, send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x" * 500, "more_body": True})
            raise RuntimeError("handler blew up mid-stream")

        middleware = BoundedCompressionMiddleware(app=failing_app, config=config)

        async def send(message) -> None:
            return None

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/stream",
            "headers": [(b"accept-encoding", b"gzip")],
            "state": {},
        }
        with pytest.raises(RuntimeError):
            await middleware(scope, None, send)

        assert config.compression_limiter.active_compressors == 0


class TestBuildCompressionConfig:
    def test_disabled_returns_none(self):
        assert build_compression_config(CompressionConfig(enabled=False)) is None

    def test_defaults_bound_memory(self):
        config = build_compression_config(CompressionConfig())

        assert config.minimum_size == 2048
        assert config.max_concurrent_compressions == DEFAULT_MAX_CONCURRENT_COMPRESSIONS
        assert config.compression_facade is SafeGzipCompression
        assert config.middleware_class is BoundedCompressionMiddleware
        assert config.exclude == [STREAMING_EXCLUDE_PATTERN]

    def test_settings_are_applied(self):
        config = build_compression_config(
            CompressionConfig(minimum_size=4096, max_concurrent=3)
        )

        assert config.minimum_size == 4096
        assert config.max_concurrent_compressions == 3

    def test_extra_excludes_keep_the_stream_path(self):
        config = build_compression_config(CompressionConfig(), exclude=["/events"])

        assert STREAMING_EXCLUDE_PATTERN in config.exclude
        assert "/events" in config.exclude


class TestCompressionSettingsSection:
    def test_section_is_registered(self):
        assert config_mod._CONFIG_SECTIONS["compression"] is CompressionConfig

    def test_defaults(self):
        settings = CompressionConfig()

        assert settings.enabled is True
        assert settings.minimum_size == 2048
        assert settings.max_concurrent == DEFAULT_MAX_CONCURRENT_COMPRESSIONS
