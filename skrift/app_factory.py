"""Shared configuration helpers for ASGI app creation.

Eliminates duplication between create_app() and create_setup_app() in asgi.py.
"""

from __future__ import annotations

import binascii
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from cryptography.exceptions import InvalidTag
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.datastructures import MutableScopeHeaders
from litestar.datastructures.cookie import Cookie
from litestar.exceptions import HTTPException
from litestar.middleware.session.client_side import (
    ClientSideSessionBackend,
    CookieBackendConfig,
)
from litestar.template import TemplateConfig

from skrift.lib.exceptions import http_exception_handler, internal_server_error_handler
from skrift.lib.markdown import render_markdown
from skrift.middleware.security import csp_nonce_var

if TYPE_CHECKING:
    from litestar.connection import ASGIConnection
    from litestar.types import Message, ScopeSession

logger = logging.getLogger(__name__)

# Shared exception handlers dict
EXCEPTION_HANDLERS: dict[type[Exception], Any] = {
    HTTPException: http_exception_handler,
    Exception: internal_server_error_handler,
}

# Module-level references for runtime updates
_jinja_env = None

# Scope key used to signal that a stale session cookie was detected
_STALE_SESSION_KEY = "_skrift_stale_session_cookie"


class _SessionBackend(ClientSideSessionBackend):
    """Session backend that cleans up stale hostname-scoped session cookies.

    When ``cookie_domain`` is configured (e.g. ``.example.com``), cookies
    previously set without a domain are scoped to the exact hostname
    (e.g. ``app.example.com``).  Because the hostname-specific cookie has
    higher specificity, the browser sends it *first* and it shadows the
    domain-scoped cookie.  The server can't decrypt the stale cookie and
    starts a fresh (empty) session on every request — breaking OAuth flows
    and anything else that relies on session continuity.

    This backend detects the decryption failure and adds a ``Set-Cookie``
    header *without* a ``Domain`` attribute, which tells the browser to
    expire the hostname-scoped cookie.
    """

    async def load_from_connection(self, connection: ASGIConnection) -> dict[str, Any]:
        if cookie_keys := self.get_cookie_keys(connection):
            data = [connection.cookies[key].encode("utf-8") for key in cookie_keys]
            try:
                return self.load_data(data)
            except (InvalidTag, binascii.Error):
                connection.scope[_STALE_SESSION_KEY] = True
        return {}

    async def store_in_message(
        self,
        scope_session: ScopeSession,
        message: Message,
        connection: ASGIConnection,
    ) -> None:
        await super().store_in_message(scope_session, message, connection)

        if not connection.scope.pop(_STALE_SESSION_KEY, False) or not self.config.domain:
            return

        # Expire the cookie without a Domain attribute so the browser
        # matches (and removes) the hostname-scoped stale cookie.
        headers = MutableScopeHeaders.from_message(message)
        clear_params = {k: v for k, v in self._clear_cookie_params.items() if k != "domain"}
        for key in self.get_cookie_key_set(connection):
            headers.add(
                "Set-Cookie",
                Cookie(value="null", key=key, expires=0, **clear_params).to_header(header=""),
            )


@dataclass
class _SessionConfig(CookieBackendConfig):
    _backend_class = _SessionBackend  # type: ignore[assignment]


def create_session_config(
    secret_key: str,
    max_age: int = 86400,
    secure: bool = False,
    cookie_domain: str | None = None,
    cookie_name: str = "session",
) -> CookieBackendConfig:
    """Create a cookie-backed session config."""
    session_secret = hashlib.sha256(secret_key.encode()).digest()
    return _SessionConfig(
        secret=session_secret,
        key=cookie_name,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        domain=cookie_domain,
    )


def get_template_directories_for_theme(theme_name: str) -> list[Path]:
    """Compute template directory list for a specific theme.

    Returns [themes/<name>/templates/, ./templates/, skrift/templates/] when
    theme is set, or [./templates/, skrift/templates/] when empty.
    """
    from skrift.lib.theme import get_themes_dir

    dirs: list[Path] = []
    if theme_name:
        theme_templates = get_themes_dir() / theme_name / "templates"
        if theme_templates.is_dir():
            dirs.append(theme_templates)

    dirs.append(Path(os.getcwd()) / "templates")
    dirs.append(Path(__file__).parent / "templates")
    return dirs


def get_template_directories() -> list[Path]:
    """Get template directories with the currently active theme applied."""
    from skrift.db.services.setting_service import get_cached_site_theme
    return get_template_directories_for_theme(get_cached_site_theme())


def update_template_directories() -> None:
    """Update the Jinja environment's search path for instant theme switching."""
    global _jinja_env
    if _jinja_env is None:
        return

    dirs = get_template_directories()
    _jinja_env.loader.searchpath = [str(d) for d in dirs]

    # Jinja caches compiled templates keyed by name.  When the searchpath
    # changes the cached entries still pass the ``auto_reload`` mtime check
    # (the old file on disk hasn't changed), so stale templates are served.
    # Flushing the cache forces a fresh lookup from the new searchpath.
    if hasattr(_jinja_env, "cache") and _jinja_env.cache is not None:
        _jinja_env.cache.clear()


def build_template_engine_callback(
    extra_globals: dict[str, Any],
    extra_filters: dict[str, Any] | None = None,
    register_for_updates: bool = True,
) -> Callable:
    """Build a template engine callback that sets globals and filters.

    Args:
        register_for_updates: When True, store this engine as the module-level
            ``_jinja_env`` so that ``update_template_directories()`` can update
            its searchpath at runtime (e.g. after theme changes).  Set to False
            for subsidiary apps (subdomain sites) whose template directories
            are fixed at creation time.
    """
    def configure_engine(engine: JinjaTemplateEngine):
        if register_for_updates:
            global _jinja_env
            _jinja_env = engine.engine

        engine.engine.globals.update({
            "now": datetime.now,
            "csp_nonce": lambda: csp_nonce_var.get(""),
            **extra_globals,
        })
        filters = {"markdown": render_markdown}
        if extra_filters:
            filters.update(extra_filters)
        engine.engine.filters.update(filters)

    return configure_engine


def create_template_config(directories: list[Path], engine_callback: Callable) -> TemplateConfig:
    """Create template config using the given template directories."""
    return TemplateConfig(
        directory=directories,
        engine=JinjaTemplateEngine,
        engine_callback=engine_callback,
    )


def create_static_hasher(
    themes_dir: Path,
    site_static_dir: Path,
    package_static_dir: Path,
):
    """Create a static URL hasher using fixed paths.

    Returns:
        A :class:`~skrift.asgi.StaticHasher` instance for generating
        cache-busted ``/static/...`` URLs in templates.
    """
    from skrift.asgi import StaticHasher

    return StaticHasher(themes_dir, site_static_dir, package_static_dir)
