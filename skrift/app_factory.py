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
from litestar.static_files import create_static_files_router
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


def get_template_directories() -> tuple[Path, Path]:
    """Get template directories: working dir first for overrides, then package dir."""
    return Path(os.getcwd()) / "templates", Path(__file__).parent / "templates"


def get_static_directories() -> tuple[Path, Path]:
    """Get static directories: working dir first for overrides, then package dir."""
    return Path(os.getcwd()) / "static", Path(__file__).parent / "static"


def build_template_engine_callback(
    extra_globals: dict[str, Any],
    extra_filters: dict[str, Any] | None = None,
) -> Callable:
    """Build a template engine callback that sets globals and filters."""
    def configure_engine(engine: JinjaTemplateEngine):
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


def create_template_config(engine_callback: Callable) -> TemplateConfig:
    """Create template config using both template directories."""
    working_dir, package_dir = get_template_directories()
    return TemplateConfig(
        directory=[working_dir, package_dir],
        engine=JinjaTemplateEngine,
        engine_callback=engine_callback,
    )


def create_static_router_and_hasher():
    """Create static files router and hasher using both static directories."""
    from skrift.asgi import StaticHasher

    working_dir, package_dir = get_static_directories()
    router = create_static_files_router(
        path="/static",
        directories=[working_dir, package_dir],
    )
    hasher = StaticHasher([working_dir, package_dir])
    return router, hasher
