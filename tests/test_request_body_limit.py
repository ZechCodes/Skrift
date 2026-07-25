"""Request bodies are capped before Litestar buffers them into memory.

Without an explicit cap a single oversized POST is fully buffered (and, for
multipart, parsed) before any application-level size check runs. The cap has to
stay above the largest upload a configured store accepts, otherwise legitimate
uploads would be rejected by the transport instead of by the storage layer.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from litestar import Litestar, post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.testing import TestClient

from skrift.config import (
    DEFAULT_MAX_REQUEST_BODY_SIZE,
    MULTIPART_FRAMING_OVERHEAD,
    Settings,
    StorageConfig,
    StoreConfig,
    resolve_request_max_body_size,
)


def build_settings(**overrides) -> Settings:
    """Settings with only the fields these tests care about."""
    values = {"secret_key": "test-secret-key"}
    values.update(overrides)
    return Settings(**values)


def build_storage(**store_sizes: int) -> StorageConfig:
    """Storage config with one store per named upload limit."""
    return StorageConfig(
        default=next(iter(store_sizes)),
        stores={
            name: StoreConfig(max_upload_size=size)
            for name, size in store_sizes.items()
        },
    )


class TestResolveRequestMaxBodySize:
    def test_defaults_leave_room_for_the_largest_upload_plus_framing(self):
        settings = build_settings()

        assert resolve_request_max_body_size(settings) == (
            settings.storage.stores["default"].max_upload_size
            + MULTIPART_FRAMING_OVERHEAD
        )

    def test_configured_floor_wins_when_it_exceeds_upload_needs(self):
        settings = build_settings(
            max_request_body_size=50_000_000,
            storage=build_storage(default=1_024),
        )

        assert resolve_request_max_body_size(settings) == 50_000_000

    def test_largest_store_limit_wins_across_stores(self):
        settings = build_settings(
            max_request_body_size=1_024,
            storage=build_storage(small=2_048, large=8_192),
        )

        assert resolve_request_max_body_size(settings) == (
            8_192 + MULTIPART_FRAMING_OVERHEAD
        )

    def test_default_floor_matches_the_documented_constant(self):
        assert build_settings().max_request_body_size == DEFAULT_MAX_REQUEST_BODY_SIZE


def build_upload_app(max_body_size: int) -> tuple[Litestar, list[int]]:
    """An app whose upload handler records the size of each body it receives."""
    received_sizes: list[int] = []

    @post("/upload")
    async def upload(
        data: Annotated[UploadFile, Body(media_type=RequestEncodingType.MULTI_PART)],
    ) -> dict:
        content = await data.read()
        received_sizes.append(len(content))
        return {"size": len(content)}

    app = Litestar(
        route_handlers=[upload],
        request_max_body_size=max_body_size,
        openapi_config=None,
    )
    return app, received_sizes


class TestRequestBodyCapEnforcement:
    def test_oversized_upload_is_rejected_before_the_handler_runs(self):
        settings = build_settings(
            max_request_body_size=1_024, storage=build_storage(default=64)
        )
        max_body_size = resolve_request_max_body_size(settings)
        app, received_sizes = build_upload_app(max_body_size)

        with TestClient(app=app) as client:
            response = client.post(
                "/upload",
                files={"data": ("big.bin", b"x" * (max_body_size + 1_024))},
            )

        assert response.status_code == 413
        assert received_sizes == []

    def test_upload_at_the_store_limit_still_fits_within_the_cap(self):
        store_limit = 2 * 1024 * 1024
        settings = build_settings(storage=build_storage(default=store_limit))
        app, received_sizes = build_upload_app(
            resolve_request_max_body_size(settings)
        )

        with TestClient(app=app) as client:
            response = client.post(
                "/upload", files={"data": ("big.bin", b"x" * store_limit)}
            )

        assert response.status_code == 201
        assert received_sizes == [store_limit]

    def test_normal_upload_is_unaffected(self):
        settings = build_settings()
        app, received_sizes = build_upload_app(
            resolve_request_max_body_size(settings)
        )

        with TestClient(app=app) as client:
            response = client.post(
                "/upload", files={"data": ("small.txt", b"hello")}
            )

        assert response.status_code == 201
        assert received_sizes == [5]


class TestSetupAppRequestCap:
    def test_setup_app_caps_request_bodies(self):
        from skrift.asgi import create_setup_app

        setup_app = create_setup_app()

        assert setup_app.app.request_max_body_size == DEFAULT_MAX_REQUEST_BODY_SIZE
