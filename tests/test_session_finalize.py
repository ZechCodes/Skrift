"""L2 + existing rotation guarantees for finalize_authenticated_session.

Pins the invariants that `finalize_authenticated_session` rotates the
session by clearing it, then repopulates with fresh user + auth state
— including a fresh CSRF token (L2) and a fresh idle-timeout stamp
(M7).
"""

from time import time
from unittest.mock import MagicMock

from skrift.auth.session_keys import (
    SESSION_AUTH_NEXT,
    SESSION_IDLE_LAST_SEEN,
    SESSION_USER_EMAIL,
    SESSION_USER_ID,
)
from skrift.auth.session_service import finalize_authenticated_session
from skrift.forms.core import CSRF_SESSION_KEY


def _request_with_session(session: dict) -> MagicMock:
    request = MagicMock()
    request.session = session
    return request


def _user(uid="user-123", email="u@example.com", name="U", picture="https://x/p"):
    user = MagicMock()
    user.id = uid
    user.email = email
    user.name = name
    user.picture_url = picture
    return user


def test_finalize_clears_then_populates_user_fields():
    request = _request_with_session({"stale_key": "dropped", "user_id": "old"})

    finalize_authenticated_session(request, _user())

    # Old state is gone; new user fields are present.
    assert "stale_key" not in request.session
    assert request.session[SESSION_USER_ID] == "user-123"
    assert request.session[SESSION_USER_EMAIL] == "u@example.com"


def test_finalize_reseeds_csrf_token():
    """L2 — the pre-login CSRF token (whatever it was) must be replaced
    by a fresh one before any authenticated form renders. Leaves no
    window where the session is authenticated without a current token."""
    pre_login_csrf = "attacker-seeded-or-just-stale"
    request = _request_with_session({CSRF_SESSION_KEY: pre_login_csrf})

    finalize_authenticated_session(request, _user())

    new_csrf = request.session[CSRF_SESSION_KEY]
    assert new_csrf != pre_login_csrf
    # `secrets.token_urlsafe(32)` → 43-char urlsafe base64 (no padding).
    assert len(new_csrf) >= 40


def test_finalize_stamps_idle_last_seen():
    """M7 regression — cover this alongside L2 since they both touch
    the same seam."""
    request = _request_with_session({})
    before = int(time())

    finalize_authenticated_session(request, _user())

    after = int(time())
    stamped = request.session[SESSION_IDLE_LAST_SEEN]
    assert before <= stamped <= after


def test_finalize_preserves_auth_next_and_flash():
    """Flash messages and the post-login `next` URL must survive the
    session clear — otherwise users lose redirect targets or success
    messages at login."""
    request = _request_with_session(
        {
            "flash": "Success",
            "flash_messages": [{"message": "Hi", "type": "info", "dismissible": True}],
            SESSION_AUTH_NEXT: "/admin",
        }
    )

    finalize_authenticated_session(request, _user())

    assert request.session["flash"] == "Success"
    assert request.session["flash_messages"][0]["message"] == "Hi"
    assert request.session[SESSION_AUTH_NEXT] == "/admin"
