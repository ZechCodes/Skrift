"""Shared configuration helpers for ASGI app creation.

Eliminates duplication between create_app() and create_setup_app() in asgi.py.
"""

import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.exceptions import HTTPException
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.template import TemplateConfig

from skrift.lib.exceptions import http_exception_handler, internal_server_error_handler
from skrift.lib.markdown import render_markdown
from skrift.middleware.security import csp_nonce_var

logger = logging.getLogger(__name__)

# Shared exception handlers dict
EXCEPTION_HANDLERS: dict[type[Exception], Any] = {
    HTTPException: http_exception_handler,
    Exception: internal_server_error_handler,
}

# Module-level references for runtime updates
_jinja_env = None
_static_dirs: list[Path] | None = None
_static_hasher = None


def create_session_config(
    secret_key: str,
    max_age: int = 86400,
    secure: bool = False,
    cookie_domain: str | None = None,
) -> CookieBackendConfig:
    """Create a cookie-backed session config."""
    session_secret = hashlib.sha256(secret_key.encode()).digest()
    return CookieBackendConfig(
        secret=session_secret,
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


def get_static_directories_for_theme(theme_name: str) -> list[Path]:
    """Compute static directory list for a specific theme."""
    from skrift.lib.theme import get_themes_dir

    dirs: list[Path] = []
    if theme_name:
        theme_static = get_themes_dir() / theme_name / "static"
        if theme_static.is_dir():
            dirs.append(theme_static)

    dirs.append(Path(os.getcwd()) / "static")
    dirs.append(Path(__file__).parent / "static")
    return dirs


def get_template_directories() -> list[Path]:
    """Get template directories with the currently active theme applied."""
    from skrift.db.services.setting_service import get_cached_site_theme
    return get_template_directories_for_theme(get_cached_site_theme())


def get_static_directories() -> list[Path]:
    """Get static directories with the currently active theme applied."""
    from skrift.db.services.setting_service import get_cached_site_theme
    return get_static_directories_for_theme(get_cached_site_theme())


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


def update_static_directories() -> None:
    """Update the static file directories for instant theme switching."""
    if _static_dirs is None:
        return

    new_dirs = get_static_directories()
    _static_dirs.clear()
    _static_dirs.extend(new_dirs)

    if _static_hasher is not None:
        _static_hasher._cache.clear()


def build_template_engine_callback(
    extra_globals: dict[str, Any],
    extra_filters: dict[str, Any] | None = None,
) -> Callable:
    """Build a template engine callback that sets globals and filters."""
    def configure_engine(engine: JinjaTemplateEngine):
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


def create_static_hasher(directories: list[Path] | None = None):
    """Create static files middleware and hasher using the given directories.

    Returns:
        A ``(middleware, hasher)`` tuple.  *middleware* is a
        :class:`~skrift.middleware.static.StaticFilesMiddleware` partial
        (wrapped in ``DefineMiddleware``), and *hasher* is a
        :class:`~skrift.asgi.StaticHasher` instance.
    """
    global _static_dirs, _static_hasher
    from litestar.middleware import DefineMiddleware
    from skrift.asgi import StaticHasher
    from skrift.middleware.static import StaticFilesMiddleware

    if directories is None:
        directories = get_static_directories()

    _static_dirs = directories

    middleware = DefineMiddleware(StaticFilesMiddleware, directories=_static_dirs)
    hasher = StaticHasher(_static_dirs)
    _static_hasher = hasher
    return middleware, hasher
