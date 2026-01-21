from pathlib import Path

from litestar import Request, Response
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_500_INTERNAL_SERVER_ERROR

from skrift.config import get_settings

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"


def _accepts_html(request: Request) -> bool:
    """Check if the request accepts HTML responses (browser request)."""
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _resolve_error_template(status_code: int) -> str:
    """Resolve error template with fallback, WP-style."""
    specific_template = f"error-{status_code}.html"
    if (TEMPLATE_DIR / specific_template).exists():
        return specific_template
    return "error.html"


def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Handle HTTP exceptions with HTML for browsers, JSON for APIs."""
    status_code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

    if _accepts_html(request):
        template_name = _resolve_error_template(status_code)
        template_engine = request.app.template_engine
        template = template_engine.get_template(template_name)
        content = template.render(
            status_code=status_code,
            message=detail,
            user=None,
            site_name=get_settings().site_name,
        )
        return Response(
            content=content,
            status_code=status_code,
            media_type="text/html",
        )

    # JSON response for API clients
    return Response(
        content={"status_code": status_code, "detail": detail},
        status_code=status_code,
        media_type="application/json",
    )


def internal_server_error_handler(request: Request, exc: Exception) -> Response:
    """Handle unexpected exceptions with HTML for browsers, JSON for APIs."""
    status_code = HTTP_500_INTERNAL_SERVER_ERROR

    if _accepts_html(request):
        template_name = _resolve_error_template(status_code)
        template_engine = request.app.template_engine
        template = template_engine.get_template(template_name)
        content = template.render(
            status_code=status_code,
            message="An unexpected error occurred.",
            user=None,
            site_name=get_settings().site_name,
        )
        return Response(
            content=content,
            status_code=status_code,
            media_type="text/html",
        )

    # JSON response for API clients
    return Response(
        content={"status_code": status_code, "detail": "Internal Server Error"},
        status_code=status_code,
        media_type="application/json",
    )
