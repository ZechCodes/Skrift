"""Tests for exception handler logging behavior."""

from unittest.mock import MagicMock, patch

import pytest

from skrift.lib import observability
from skrift.lib.exceptions import internal_server_error_handler


@pytest.fixture
def fake_request():
    """Create a minimal mock request for the error handler."""
    request = MagicMock()
    request.method = "GET"
    request.url.path = "/test"
    request.headers.get.return_value = "application/json"
    return request


class TestObservabilityException:
    """Test the observability.exception() facade function."""

    def test_returns_true_when_available(self):
        with patch.object(observability, "_logfire", MagicMock()) as mock_lf, \
             patch.object(observability, "_configured", True):
            result = observability.exception("test error")
            assert result is True
            mock_lf.exception.assert_called_once_with("test error")

    def test_returns_false_when_unavailable(self):
        with patch.object(observability, "_logfire", None), \
             patch.object(observability, "_configured", False):
            result = observability.exception("test error")
            assert result is False

    def test_passes_kwargs_to_logfire(self):
        with patch.object(observability, "_logfire", MagicMock()) as mock_lf, \
             patch.object(observability, "_configured", True):
            observability.exception("error on {method}", method="POST")
            mock_lf.exception.assert_called_once_with("error on {method}", method="POST")


class TestInternalServerErrorHandler:
    """Test that internal_server_error_handler logs exceptions."""

    def test_calls_observability_when_available(self, fake_request):
        exc = RuntimeError("boom")
        with patch.object(observability, "exception", return_value=True) as mock_exc:
            response = internal_server_error_handler(fake_request, exc)

        mock_exc.assert_called_once_with(
            "Unhandled exception on {method} {path}",
            method="GET",
            path="/test",
        )
        assert response.status_code == 500

    def test_falls_back_to_stdlib_when_unavailable(self, fake_request):
        exc = RuntimeError("boom")
        with patch.object(observability, "exception", return_value=False), \
             patch("skrift.lib.exceptions.logger") as mock_logger:
            response = internal_server_error_handler(fake_request, exc)

        mock_logger.exception.assert_called_once_with(
            "Unhandled exception on %s %s", "GET", "/test",
        )
        assert response.status_code == 500

    def test_does_not_double_log(self, fake_request):
        """When observability handles it, stdlib logger should NOT be called."""
        exc = RuntimeError("boom")
        with patch.object(observability, "exception", return_value=True), \
             patch("skrift.lib.exceptions.logger") as mock_logger:
            internal_server_error_handler(fake_request, exc)

        mock_logger.exception.assert_not_called()

    def test_returns_500_json_for_api_clients(self, fake_request):
        exc = RuntimeError("boom")
        with patch.object(observability, "exception", return_value=False), \
             patch("skrift.lib.exceptions.logger"):
            response = internal_server_error_handler(fake_request, exc)

        assert response.status_code == 500
        assert response.content == {"status_code": 500, "detail": "Internal Server Error"}
