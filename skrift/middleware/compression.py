"""Memory-bounded gzip compression for Litestar.

Litestar's stock compression middleware allocates a zlib deflate state for
every in-flight request that advertises ``accept-encoding: gzip`` — roughly
256 KB of address space and ~153 KB of touched RSS each, regardless of the
compression level.  Those blocks are never returned to the OS, so a burst of
concurrent requests permanently ratchets process RSS.  Two behaviours here
keep that bounded:

* :class:`ConcurrentCompressionLimiter` caps how many deflate states may be
  alive process-wide.  Requests arriving past the cap are answered
  *uncompressed* — never queued — so a slow client can never head-of-line
  block anyone else.
* The compressor is created lazily, only once the response is known to be
  compressible (streaming, or a buffered body of at least ``minimum_size``
  bytes), so short responses cost nothing at all.

:class:`SafeGzipCompression` additionally works around a Python 3.13
regression where ``GzipFile``'s finalizer flushes into an already-closed
``BytesIO`` — which happens when a client disconnects before the response is
fully written — raising ``ValueError: I/O operation on closed file``.  It also
flushes the deflate stream only when a streamed chunk has to reach the client
immediately; Litestar's facade flushes on every write, which forces the whole
pending window to be materialized even for buffered responses.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from gzip import GzipFile
from io import BytesIO
from typing import TYPE_CHECKING, Literal

from litestar.config.compression import CompressionConfig as LitestarCompressionConfig
from litestar.datastructures import Headers, MutableScopeHeaders
from litestar.enums import CompressionEncoding
from litestar.middleware import DefineMiddleware
from litestar.middleware.compression import CompressionMiddleware
from litestar.middleware.compression.facade import CompressionFacade
from litestar.utils.empty import value_or_default
from litestar.utils.scope.state import ScopeState

if TYPE_CHECKING:
    from litestar.types import (
        ASGIApp,
        HTTPResponseStartEvent,
        Message,
        Receive,
        Scope,
        Send,
    )

    from skrift.config import CompressionConfig

__all__ = (
    "DEFAULT_MAX_CONCURRENT_COMPRESSIONS",
    "DEFAULT_MINIMUM_COMPRESSED_SIZE",
    "STREAMING_EXCLUDE_PATTERN",
    "BoundedCompressionConfig",
    "BoundedCompressionMiddleware",
    "BoundedCompressionSend",
    "ConcurrentCompressionLimiter",
    "SafeGzipCompression",
    "build_compression_config",
    "build_compression_middleware",
    "process_compression_limiter",
)

DEFAULT_MAX_CONCURRENT_COMPRESSIONS = 20
"""Live deflate states allowed at once (~153 KB each, so ~3 MB worst case)."""

DEFAULT_MINIMUM_COMPRESSED_SIZE = 2048
"""Bodies smaller than this are not worth a compressor; Litestar defaults to 500."""

STREAMING_EXCLUDE_PATTERN = "/notifications/stream"
"""Server-sent events endpoint; the compression middleware must not buffer it."""


class ConcurrentCompressionLimiter:
    """Process-wide bound on simultaneously live compressor states.

    Acquisition never blocks: callers that cannot get a slot send their
    response uncompressed instead of waiting, which keeps slow readers from
    holding compressor memory hostage or head-of-line blocking other responses.
    """

    __slots__ = ("_active_compressors", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_compressors = 0

    @property
    def active_compressors(self) -> int:
        """How many compressor states are currently checked out."""
        with self._lock:
            return self._active_compressors

    def try_acquire(self, max_concurrent_compressions: int) -> bool:
        """Claim a slot if one is free, returning whether it was claimed."""
        with self._lock:
            if self._active_compressors >= max_concurrent_compressions:
                return False

            self._active_compressors += 1
            return True

    def release(self) -> None:
        """Return a previously claimed slot."""
        with self._lock:
            if self._active_compressors == 0:
                raise RuntimeError("Released a compression slot that was never acquired")

            self._active_compressors -= 1


process_compression_limiter = ConcurrentCompressionLimiter()
"""The limiter shared by every app in this process (primary, sites, setup)."""


@dataclass
class BoundedCompressionConfig(LitestarCompressionConfig):
    """Litestar compression config extended with the concurrency bound."""

    max_concurrent_compressions: int = DEFAULT_MAX_CONCURRENT_COMPRESSIONS
    """Maximum number of compressor states alive at once, process-wide."""

    compression_limiter: ConcurrentCompressionLimiter = field(
        default=process_compression_limiter
    )
    """Limiter enforcing :attr:`max_concurrent_compressions`."""


class SafeGzipCompression(CompressionFacade):
    """Gzip facade that owns a compression slot and defers flushing.

    Differences from ``litestar.middleware.compression.gzip_facade.GzipCompression``:

    * :meth:`acquire` refuses to build a compressor once the configured
      concurrency cap is reached, and :meth:`close` always returns the slot.
    * :meth:`write` accumulates instead of flushing; callers that stream ask
      for the pending bytes explicitly through :meth:`flush`.
    * :meth:`close` swallows the ``ValueError`` raised by Python 3.13's
      ``GzipFile`` finalizer when the output buffer is already closed.
    """

    __slots__ = ("buffer", "closed", "compression_encoding", "compressor", "limiter")

    encoding = CompressionEncoding.GZIP

    def __init__(
        self,
        buffer: BytesIO,
        compression_encoding: Literal[CompressionEncoding.GZIP] | str,
        config: LitestarCompressionConfig,
        limiter: ConcurrentCompressionLimiter | None = None,
    ) -> None:
        self.limiter = limiter
        self.closed = False
        self.buffer = buffer
        self.compression_encoding = compression_encoding
        self.compressor = GzipFile(
            mode="wb", fileobj=buffer, compresslevel=config.gzip_compress_level
        )

    @classmethod
    def acquire(
        cls,
        buffer: BytesIO,
        compression_encoding: Literal[CompressionEncoding.GZIP] | str,
        config: BoundedCompressionConfig,
    ) -> SafeGzipCompression | None:
        """Build a facade holding a compression slot, or ``None`` past the cap."""
        limiter = config.compression_limiter
        if not limiter.try_acquire(config.max_concurrent_compressions):
            return None

        try:
            return cls(buffer, compression_encoding, config, limiter=limiter)
        except BaseException:
            limiter.release()
            raise

    def write(self, body: bytes) -> None:
        """Feed bytes to the compressor without forcing a flush."""
        self.compressor.write(body)

    def flush(self) -> None:
        """Push everything written so far into the output buffer."""
        self.compressor.flush()

    def close(self) -> None:
        """Finish the gzip stream and release the compression slot.

        Safe to call more than once; the slot is released exactly once, even
        when the buffer was closed underneath the compressor.
        """
        if self.closed:
            return

        self.closed = True
        try:
            self.compressor.close()
        except ValueError:
            # Python 3.13: the GzipFile finalizer writes into a buffer the
            # middleware already closed after a client disconnect.
            pass
        finally:
            if self.limiter is not None:
                limiter = self.limiter
                self.limiter = None
                limiter.release()


class BoundedCompressionSend:
    """Per-response ASGI ``send`` wrapper that compresses within the slot budget.

    Mirrors Litestar's compression send wrapper with three changes: the
    compressor is created lazily and only when a slot is available, responses
    that cannot get a slot are sent as-is, and the deflate stream is flushed
    only for streamed chunks.
    """

    __slots__ = (
        "_buffer",
        "_compression_encoding",
        "_config",
        "_connection_state",
        "_facade",
        "_initial_message",
        "_send",
        "_started",
    )

    def __init__(
        self,
        send: Send,
        compression_encoding: CompressionEncoding | str,
        scope: Scope,
        config: BoundedCompressionConfig,
    ) -> None:
        self._send = send
        self._compression_encoding = compression_encoding
        self._config = config
        self._buffer = BytesIO()
        self._facade: SafeGzipCompression | None = None
        self._initial_message: HTTPResponseStartEvent | None = None
        self._started = False
        self._connection_state = ScopeState.from_scope(scope)

    async def __call__(self, message: Message) -> None:
        """Handle one outgoing ASGI message."""
        if message["type"] == "http.response.start":
            self._initial_message = message
            return

        if self._initial_message is None:
            await self._send(message)
            return

        if value_or_default(self._connection_state.is_cached, False):
            await self._send(self._initial_message)
            await self._send(message)
            self.close()
            return

        if message["type"] == "http.disconnect":
            self.close()
            return

        if message["type"] != "http.response.body":
            await self._send(message)
            return

        if self._started:
            await self._send_streamed_chunk(message)
            return

        self._started = True
        if message.get("more_body"):
            await self._start_streaming_response(message)
        elif len(message["body"]) >= self._config.minimum_size:
            await self._send_compressed_response(message)
        else:
            await self._send_uncompressed_response(message)

    def close(self) -> None:
        """Close the compressor, if one was ever created, releasing its slot."""
        if self._facade is not None:
            self._facade.close()

    async def _start_streaming_response(self, message: Message) -> None:
        """Send the first chunk of a streamed response, compressed when possible."""
        self._facade = self._config.compression_facade.acquire(
            self._buffer, self._compression_encoding, self._config
        )
        if self._facade is None:
            await self._send_uncompressed_response(message)
            return

        headers = MutableScopeHeaders(self._initial_message)
        headers["Content-Encoding"] = self._compression_encoding
        headers.extend_header_value("vary", "Accept-Encoding")
        del headers["Content-Length"]
        self._connection_state.response_compressed = True

        self._facade.write(message["body"])
        self._facade.flush()
        await self._send(self._initial_message)
        await self._send({**message, "body": self._drain_buffer()})

    async def _send_compressed_response(self, message: Message) -> None:
        """Compress and send a complete buffered body."""
        self._facade = self._config.compression_facade.acquire(
            self._buffer, self._compression_encoding, self._config
        )
        if self._facade is None:
            await self._send_uncompressed_response(message)
            return

        self._facade.write(message["body"])
        self._facade.close()
        body = self._drain_buffer()

        headers = MutableScopeHeaders(self._initial_message)
        headers["Content-Encoding"] = self._compression_encoding
        headers["Content-Length"] = str(len(body))
        headers.extend_header_value("vary", "Accept-Encoding")
        self._connection_state.response_compressed = True

        await self._send(self._initial_message)
        await self._send({**message, "body": body})

    async def _send_uncompressed_response(self, message: Message) -> None:
        """Pass a response through untouched (too small, or no slot available)."""
        await self._send(self._initial_message)
        await self._send(message)

    async def _send_streamed_chunk(self, message: Message) -> None:
        """Send a follow-up chunk of an already-started response."""
        if self._facade is None:
            await self._send(message)
            return

        more_body = message.get("more_body")
        self._facade.write(message["body"])
        if more_body:
            self._facade.flush()
        else:
            self._facade.close()

        body = self._drain_buffer()
        if not more_body:
            self._buffer.close()

        await self._send({**message, "body": body})

    def _drain_buffer(self) -> bytes:
        """Take everything written to the output buffer so far."""
        compressed = self._buffer.getvalue()
        self._buffer.seek(0)
        self._buffer.truncate()
        return compressed


class BoundedCompressionMiddleware(CompressionMiddleware):
    """Compression middleware that bounds live compressor memory.

    Litestar's middleware hardcodes its own gzip facade and builds it for every
    request that accepts gzip; this one always uses the configured facade and
    guarantees the compressor is closed — and its slot released — even when the
    application raises mid-response.
    """

    def __init__(self, app: ASGIApp, config: BoundedCompressionConfig) -> None:
        super().__init__(app=app, config=config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap ``send`` so compressible responses are compressed within budget."""
        accept_encoding = Headers.from_scope(scope).get("accept-encoding", "")
        facade_encoding = self.config.compression_facade.encoding

        if facade_encoding in accept_encoding:
            compression_encoding: CompressionEncoding | str = facade_encoding
        elif self.config.gzip_fallback and CompressionEncoding.GZIP in accept_encoding:
            compression_encoding = CompressionEncoding.GZIP
        else:
            await self.app(scope, receive, send)
            return

        compressing_send = self.create_compression_send_wrapper(
            send=send, compression_encoding=compression_encoding, scope=scope
        )
        try:
            await self.app(scope, receive, compressing_send)
        finally:
            compressing_send.close()

    def create_compression_send_wrapper(
        self,
        send: Send,
        compression_encoding: CompressionEncoding | str,
        scope: Scope,
    ) -> BoundedCompressionSend:
        """Build the per-response send wrapper."""
        return BoundedCompressionSend(
            send=send,
            compression_encoding=compression_encoding,
            scope=scope,
            config=self.config,
        )


def build_compression_config(
    compression_settings: CompressionConfig,
    exclude: str | list[str] | None = None,
) -> BoundedCompressionConfig | None:
    """Build an app's Litestar compression config, or ``None`` when disabled.

    ``exclude`` is added on top of :data:`STREAMING_EXCLUDE_PATTERN`, which is
    always excluded so server-sent event streams are never buffered by the
    compression middleware.
    """
    if not compression_settings.enabled:
        return None

    excluded_paths = [STREAMING_EXCLUDE_PATTERN]
    if isinstance(exclude, str):
        excluded_paths.append(exclude)
    elif exclude:
        excluded_paths.extend(exclude)

    return BoundedCompressionConfig(
        backend="gzip",
        compression_facade=SafeGzipCompression,
        middleware_class=BoundedCompressionMiddleware,
        minimum_size=compression_settings.minimum_size,
        max_concurrent_compressions=compression_settings.max_concurrent,
        exclude=excluded_paths,
    )


def build_compression_middleware(
    compression_settings: CompressionConfig,
    exclude: str | list[str] | None = None,
) -> list[DefineMiddleware]:
    """Build an app's compression middleware, empty when compression is disabled.

    Compression is installed as application middleware rather than through
    Litestar's ``compression_config``: Litestar hardcodes its own middleware and
    gzip facade for the ``gzip`` backend, so a configured facade is never used.
    Place this first in the middleware list so it wraps ``send`` outermost and
    sees the final response bytes.
    """
    config = build_compression_config(compression_settings, exclude)
    if config is None:
        return []

    return [DefineMiddleware(BoundedCompressionMiddleware, config=config)]
