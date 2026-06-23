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


class TestHtmlErrorPageRendering:
    """The HTML error page extends base.html, whose masthead renders
    ``{{ csrf_field() }}`` inside the logout form for logged-in users. The
    production ``csrf_field`` global reads ``context["request"]``, but the
    exception handler renders the template outside the request/session
    middleware. The handler must supply a ``csrf_field`` that works without a
    request, or the error page itself blows up into a second 500."""

    @staticmethod
    def _request_with_csrf_global_engine(template_src: str):
        """A request whose template engine mimics production: a ``csrf_field``
        global that reads ``context["request"]`` (and so raises if ``request``
        is absent from the render context)."""
        from jinja2 import DictLoader, Environment, pass_context

        from skrift.forms.core import csrf_field as production_csrf_field

        environment = Environment(
            loader=DictLoader({"error.html": template_src}), autoescape=True
        )

        @pass_context
        def _csrf_field_ctx(context):
            return production_csrf_field(context["request"])

        environment.globals["csrf_field"] = _csrf_field_ctx

        request = MagicMock()
        request.headers.get.return_value = "text/html"
        request.app.template_engine.get_template.return_value = environment.get_template(
            "error.html"
        )
        return request

    def test_logged_in_error_page_renders_csrf_field(self):
        from skrift.auth.session_keys import SESSION_USER_ID
        from skrift.forms.core import CSRF_SESSION_KEY
        from skrift.lib.exceptions import _render_error_response

        template_src = "<nav>{% if user %}{{ csrf_field() }}{% endif %}</nav>"
        request = self._request_with_csrf_global_engine(template_src)
        session = {SESSION_USER_ID: "user-1", CSRF_SESSION_KEY: "token-abc"}

        with patch(
            "skrift.lib.exceptions._get_session_from_cookie", return_value=session
        ), patch(
            "skrift.lib.exceptions._resolve_error_template", return_value="error.html"
        ):
            response = _render_error_response(request, 401, "Not authorized")

        body = str(response.content)
        assert response.status_code == 401
        assert 'name="_csrf"' in body
        assert "token-abc" in body

    def test_anonymous_error_page_renders_without_request(self):
        from skrift.lib.exceptions import _render_error_response

        template_src = "<nav>{% if user %}{{ csrf_field() }}{% endif %}</nav>"
        request = self._request_with_csrf_global_engine(template_src)

        with patch(
            "skrift.lib.exceptions._get_session_from_cookie", return_value=None
        ), patch(
            "skrift.lib.exceptions._resolve_error_template", return_value="error.html"
        ):
            response = _render_error_response(request, 404, "Not found")

        assert response.status_code == 404
