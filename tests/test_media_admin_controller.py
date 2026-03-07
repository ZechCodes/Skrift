"""Tests for media admin error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMediaAdminController:
    @pytest.mark.asyncio
    async def test_upload_media_uses_generic_flash_on_unexpected_error(self):
        from skrift.admin.media import MediaAdminController

        controller = MediaAdminController(owner=MagicMock())
        request = MagicMock()
        request.app.state.storage_manager = MagicMock()
        request.user = MagicMock(id="user-id")
        db_session = AsyncMock()
        upload = MagicMock()
        upload.read = AsyncMock(return_value=b"content")
        upload.filename = "file.png"
        upload.content_type = "image/png"

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
        upload = MagicMock()
        upload.read = AsyncMock(return_value=b"content")
        upload.filename = "file.png"
        upload.content_type = "image/png"

        with patch("skrift.admin.media.upload_asset", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("skrift.admin.media.logger.exception") as mock_log:
            response = await MediaAdminController.upload_media_json.fn(
                controller, request, db_session, upload
            )

        assert response.status_code == 500
        assert response.content == {"error": "Upload failed. Check the server logs."}
        mock_log.assert_called_once()
