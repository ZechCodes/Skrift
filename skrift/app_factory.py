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
