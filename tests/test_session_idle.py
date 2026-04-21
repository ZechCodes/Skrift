"""M7 — SessionIdleMiddleware: rolling idle timeout for authenticated sessions.

Covers the no-op paths (disabled, unauthenticated, non-HTTP scopes),
the stamp-refresh path, the throttle that skips back-to-back refreshes,
and the eviction-plus-flash path when the idle window is exceeded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from skrift.auth.session_keys import SESSION_IDLE_LAST_SEEN, SESSION_USER_ID
from skrift.middleware.session_idle import SessionIdleMiddleware


class _StubApp:
    """Records scope on call so tests can inspect what the middleware passed through."""

    def __init__(self) -> None:
        self.called_with_scope: dict[str, Any] | None = None

    async def __call__(self, scope, receive, send):
        self.called_with_scope = scope


def _scope(session: dict | None, scope_type: str = "http") -> dict:
    return {"type": scope_type, "session": session}


async def _noop_receive() -> Any:  # pragma: no cover — tests never call it
    return {}


async def _noop_send(_message) -> None:  # pragma: no cover — tests never call it
    return None


@pytest.mark.asyncio
async def test_disabled_when_idle_timeout_is_zero():
    """idle_timeout=0 is the documented "feature off" knob."""
    app = _StubApp()
    mw = SessionIdleMiddleware(app, idle_timeout=0)
    session = {SESSION_USER_ID: "u", SESSION_IDLE_LAST_SEEN: 0}
    scope = _scope(session)

    await mw(scope, _noop_receive, _noop_send)

    # Untouched — the old stamp stays, session.clear() never called.
    assert session[SESSION_USER_ID] == "u"
    assert session[SESSION_IDLE_LAST_SEEN] == 0
    assert app.called_with_scope is scope


@pytest.mark.asyncio
async def test_passes_through_non_http_scope_untouched():
    app = _StubApp()
    mw = SessionIdleMiddleware(app, idle_timeout=600)
    session = {SESSION_USER_ID: "u", SESSION_IDLE_LAST_SEEN: 0}
    scope = _scope(session, scope_type="lifespan")

    await mw(scope, _noop_receive, _noop_send)

    assert session[SESSION_IDLE_LAST_SEEN] == 0


@pytest.mark.asyncio
async def test_unauthenticated_session_is_ignored():
    """No user_id → middleware must not touch the session."""
    app = _StubApp()
    mw = SessionIdleMiddleware(app, idle_timeout=600)
    session: dict = {"flash_messages": [{"message": "hi", "type": "info", "dismissible": True}]}

    await mw(_scope(session), _noop_receive, _noop_send)

    assert SESSION_IDLE_LAST_SEEN not in session
    assert session["flash_messages"] == [
        {"message": "hi", "type": "info", "dismissible": True}
    ]


@pytest.mark.asyncio
async def test_recently_active_session_is_not_rewritten():
    """Activity within `stamp_interval` must not bump the stamp — cookie
    churn reduction is the whole point of the throttle."""
    app = _StubApp()
    mw = SessionIdleMiddleware(app, idle_timeout=600)  # stamp_interval = 60s
    session = {SESSION_USER_ID: "u", SESSION_IDLE_LAST_SEEN: 1_000_000}

    with patch("skrift.middleware.session_idle.time", return_value=1_000_030):
        await mw(_scope(session), _noop_receive, _noop_send)

    # 30s elapsed, below the 60s threshold — stamp unchanged.
    assert session[SESSION_IDLE_LAST_SEEN] == 1_000_000


@pytest.mark.asyncio
async def test_stamp_refreshes_past_the_throttle_interval():
    app = _StubApp()
    mw = SessionIdleMiddleware(app, idle_timeout=600)  # stamp_interval = 60s
    session = {SESSION_USER_ID: "u", SESSION_IDLE_LAST_SEEN: 1_000_000}

    with patch("skrift.middleware.session_idle.time", return_value=1_000_090):
        await mw(_scope(session), _noop_receive, _noop_send)

    # 90s elapsed, above the 60s throttle — stamp rolls forward.
    assert session[SESSION_IDLE_LAST_SEEN] == 1_000_090
    # Session is still authenticated.
    assert session[SESSION_USER_ID] == "u"


@pytest.mark.asyncio
async def test_idle_past_window_clears_session_and_adds_flash():
    app = _StubApp()
    mw = SessionIdleMiddleware(app, idle_timeout=600)
    session = {
        SESSION_USER_ID: "u",
        "user_email": "u@example.com",
        SESSION_IDLE_LAST_SEEN: 1_000_000,
        "flash_messages": [
            {"message": "existing", "type": "info", "dismissible": True}
        ],
    }

    # 601s of idleness is just past the window.
    with patch("skrift.middleware.session_idle.time", return_value=1_000_601):
        await mw(_scope(session), _noop_receive, _noop_send)

    # User data gone.
    assert SESSION_USER_ID not in session
    assert "user_email" not in session
    assert SESSION_IDLE_LAST_SEEN not in session
    # Pre-existing flash preserved, plus the new idle-logout message.
    assert session["flash_messages"][0]["message"] == "existing"
    assert session["flash_messages"][-1]["message"].startswith(
        "You've been signed out"
    )
    assert session["flash_messages"][-1]["type"] == "info"


@pytest.mark.asyncio
async def test_missing_stamp_is_treated_as_expired():
    """An authenticated session without any last_seen stamp (e.g. from
    a session minted before M7 shipped, or hand-edited) is evicted on
    the next authenticated request. Prevents a silent bypass."""
    app = _StubApp()
    mw = SessionIdleMiddleware(app, idle_timeout=600)
    session = {SESSION_USER_ID: "u"}

    await mw(_scope(session), _noop_receive, _noop_send)

    assert SESSION_USER_ID not in session
    assert session["flash_messages"][0]["message"].startswith("You've been signed out")


@pytest.mark.asyncio
async def test_stamp_interval_floor_is_60_seconds():
    """Small idle windows still get at least a 60s stamp-throttle."""
    mw = SessionIdleMiddleware(_StubApp(), idle_timeout=120)  # 120/20 = 6
    assert mw.stamp_interval == 60


@pytest.mark.asyncio
async def test_stamp_interval_scales_with_large_idle_windows():
    """Large windows get proportional throttle (5% of the window)."""
    mw = SessionIdleMiddleware(_StubApp(), idle_timeout=7200)  # 2h → 360s
    assert mw.stamp_interval == 360
