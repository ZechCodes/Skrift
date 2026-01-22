import hashlib
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


def _get_session_from_cookie(request: Request) -> dict | None:
    """Manually decode session from cookie when middleware hasn't run.

    This replicates Litestar's ClientSideSessionBackend decryption logic synchronously.
    """
    try:
        import time
        from base64 import b64decode
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from litestar.middleware.session.client_side import decode_json, NONCE_SIZE, AAD

        # Get the session cookie
        cookie_value = request.cookies.get("session")
        if not cookie_value:
            return None

        # Get the secret key and hash it the same way asgi.py does
        settings = get_settings()
        secret = hashlib.sha256(settings.secret_key.encode()).digest()

        # Decode the base64 cookie value
        decoded = b64decode(cookie_value)

        # Extract nonce (first 12 bytes)
        nonce = decoded[:NONCE_SIZE]

        # Find where AAD marker starts
        aad_starts_from = decoded.find(AAD)
        if aad_starts_from == -1:
            return None

        # Associated data is the JSON part only (after removing the AAD prefix)
        associated_data = decoded[aad_starts_from:].replace(AAD, b"")

        # Check expiration
        if decode_json(value=associated_data)["expires_at"] <= round(time.time()):
            return None

        # Extract encrypted session (between nonce and AAD marker)
        encrypted_session = decoded[NONCE_SIZE:aad_starts_from]

        # Decrypt using AES-GCM with the JSON part as associated_data
        aesgcm = AESGCM(secret)
        decrypted = aesgcm.decrypt(nonce, encrypted_session, associated_data=associated_data)

        # Deserialize JSON
        session_data = decode_json(decrypted)
        return session_data
    except Exception:
        return None


def _get_user_context_from_session(session: dict | None) -> dict | None:
    """Get user display info from session if available."""
    if not session:
        return None
    try:
        user_id = session.get("user_id")
        if not user_id:
            return None
        return {
            "id": user_id,
            "name": session.get("user_name"),
            "email": session.get("user_email"),
            "picture_url": session.get("user_picture_url"),
        }
    except Exception:
        return None


class SessionUser:
    """Lightweight user object for templates, populated from session data."""

    def __init__(self, data: dict):
        self.id = data.get("id")
        self.name = data.get("name")
        self.email = data.get("email")
        self.picture_url = data.get("picture_url")


def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Handle HTTP exceptions with HTML for browsers, JSON for APIs."""
    status_code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

    if _accepts_html(request):
        session = _get_session_from_cookie(request)
        user_data = _get_user_context_from_session(session)
        user = SessionUser(user_data) if user_data else None
        template_name = _resolve_error_template(status_code)
        template_engine = request.app.template_engine
        template = template_engine.get_template(template_name)
        content = template.render(
            status_code=status_code,
            message=detail,
            user=user,
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
        session = _get_session_from_cookie(request)
        user_data = _get_user_context_from_session(session)
        user = SessionUser(user_data) if user_data else None
        template_name = _resolve_error_template(status_code)
        template_engine = request.app.template_engine
        template = template_engine.get_template(template_name)
        content = template.render(
            status_code=status_code,
            message="An unexpected error occurred.",
            user=user,
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
