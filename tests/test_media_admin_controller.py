"""Tests for media admin error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_upload(call_log: list[str] | None = None) -> MagicMock:
    """A mock UploadFile that records the order of its read/close calls."""
    log = call_log if call_log is not None else []
    upload = MagicMock()

    async def read() -> bytes:
        log.append("read")
        return b"content"

    async def close() -> None:
        log.append("close")

    upload.read = AsyncMock(side_effect=read)
    upload.close = AsyncMock(side_effect=close)
    upload.filename = "file.png"
    upload.content_type = "image/png"
    return upload


class TestMediaUploadBufferRelease:
    @pytest.mark.asyncio
    async def test_upload_media_releases_the_spooled_buffer_after_reading(self):
        from skrift.admin.media import MediaAdminController

        controller = MediaAdminController(owner=MagicMock())
        request = MagicMock()
        request.app.state.storage_manager = MagicMock()
        request.user = MagicMock(id="user-id")
        db_session = AsyncMock()
        call_log: list[str] = []
        upload = make_upload(call_log)

        with patch("skrift.admin.media.upload_asset", new_callable=AsyncMock), \
             patch("skrift.admin.media.flash_success"):
            await MediaAdminController.upload_media.fn(
                controller, request, db_session, upload
            )

        assert call_log == ["read", "close"]

    @pytest.mark.asyncio
    async def test_upload_media_json_releases_the_spooled_buffer_after_reading(self):
        from skrift.admin.media import MediaAdminController

        controller = MediaAdminController(owner=MagicMock())
        request = MagicMock()
        request.app.state.storage_manager = MagicMock()
        request.session = {"user_id": "00000000-0000-0000-0000-000000000000"}
        db_session = AsyncMock()
        call_log: list[str] = []
        upload = make_upload(call_log)

        with patch("skrift.admin.media.upload_asset", new_callable=AsyncMock), \
             patch("skrift.admin.media.get_asset_url", new_callable=AsyncMock):
            await MediaAdminController.upload_media_json.fn(
                controller, request, db_session, upload
            )

        assert call_log == ["read", "close"]


class TestMediaAdminController:
    @pytest.mark.asyncio
    async def test_upload_media_uses_generic_flash_on_unexpected_error(self):
        from skrift.admin.media import MediaAdminController

        controller = MediaAdminController(owner=MagicMock())
        request = MagicMock()
        request.app.state.storage_manager = MagicMock()
        request.user = MagicMock(id="user-id")
        db_session = AsyncMock()
        upload = make_upload()

        with patch("skrift.admin.media.upload_asset", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("skrift.admin.media.flash_error") as mock_flash, \
             patch("skrift.admin.media.logger.exception") as mock_log:
            result = await MediaAdminController.upload_media.fn(
                controller, request, db_session, upload
            )

        assert result.url == "/admin/media"
        mock_flash.assert_called_once_with(
            request, "Upload failed. Check the server logs and try again."
        )
        mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_media_json_uses_generic_error_on_unexpected_error(self):
        from skrift.admin.media import MediaAdminController

        controller = MediaAdminController(owner=MagicMock())
        request = MagicMock()
        request.app.state.storage_manager = MagicMock()
        request.session = {"user_id": "00000000-0000-0000-0000-000000000000"}
        db_session = AsyncMock()
        upload = make_upload()

        with patch("skrift.admin.media.upload_asset", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("skrift.admin.media.logger.exception") as mock_log:
            response = await MediaAdminController.upload_media_json.fn(
                controller, request, db_session, upload
            )

        assert response.status_code == 500
        assert response.content == {"error": "Upload failed. Check the server logs."}
        mock_log.assert_called_once()
