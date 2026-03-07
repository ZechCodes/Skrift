"""Tests for ASGI dispatcher error rendering."""

from unittest.mock import MagicMock

import pytest


class TestAppDispatcherErrorResponse:
    @pytest.mark.asyncio
    async def test_hides_internal_startup_error_message(self):
        from skrift.asgi import AppDispatcher

        template = MagicMock()
        template.render.return_value = "rendered"
        setup_app = MagicMock()
        setup_app.template_engine.get_template.return_value = template
        dispatcher = AppDispatcher(setup_app=setup_app)

        sent = []

        async def send(message):
            sent.append(message)

        await dispatcher._error_response(send, "secret_key missing")

        assert template.render.call_args.kwargs["message"] == (
            "Application failed to start. Check the server logs for details."
        )
        assert template.render.call_args.kwargs["hint"] is not None
        assert sent[0]["status"] == 500
