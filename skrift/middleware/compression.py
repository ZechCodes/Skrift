"""Safe gzip compression facade for Litestar.

Python 3.13 introduced a regression where GzipFile.__del__ tries to flush
to an already-closed BytesIO buffer, raising ``ValueError: I/O operation
on closed file``.  This happens when the client disconnects before the
response is fully written — the middleware closes the buffer, but the
GzipFile finalizer still tries to write.

This module provides a drop-in replacement that catches the harmless error.
"""

from __future__ import annotations

from gzip import GzipFile
from typing import TYPE_CHECKING, Literal

from litestar.enums import CompressionEncoding
from litestar.middleware.compression.facade import CompressionFacade

if TYPE_CHECKING:
    from io import BytesIO

    from litestar.config.compression import CompressionConfig


class SafeGzipCompression(CompressionFacade):
    """GzipCompression that suppresses ValueError on close.

    Drop-in replacement for ``litestar.middleware.compression.gzip_facade.GzipCompression``
    that catches the ``ValueError: I/O operation on closed file`` error
    raised by Python 3.13's ``GzipFile`` finalizer.
    """

    __slots__ = ("buffer", "compression_encoding", "compressor")

    encoding = CompressionEncoding.GZIP

    def __init__(
        self,
        buffer: BytesIO,
        compression_encoding: Literal[CompressionEncoding.GZIP] | str,
        config: CompressionConfig,
    ) -> None:
        self.buffer = buffer
        self.compression_encoding = compression_encoding
        self.compressor = GzipFile(
            mode="wb", fileobj=buffer, compresslevel=config.gzip_compress_level
        )

    def write(self, body: bytes) -> None:
        self.compressor.write(body)
        self.compressor.flush()

    def close(self) -> None:
        try:
            self.compressor.close()
        except ValueError:
            pass
