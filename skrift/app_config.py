"""Application configuration helpers for Skrift.

This module extracts database, session, middleware, template, and static file
configuration out of asgi.py so each concern lives in its own function.
"""

import hashlib
import os
from datetime import datetime
from pathlib import Path

from advanced_alchemy.config import EngineConfig
from advanced_alchemy.extensions.litestar import AsyncSessionConfig
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.middleware import DefineMiddleware
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.static_files import create_static_files_router
from litestar.template import TemplateConfig

from skrift.config import Settings
from skrift.db.session import SafeSQLAlchemyAsyncConfig
from skrift.db.base import Base
from skrift.db.services.setting_service import (
    get_cached_site_name,
    get_cached_site_tagline,
    get_cached_site_copyright_holder,
    get_cached_site_copyright_start_year,
)
from skrift.lib.markdown import render_markdown
from skrift.middleware.rate_limit import RateLimitMiddleware
from skrift.middleware.security import SecurityHeadersMiddleware, csp_nonce_var


def build_db_config(settings: Settings) -> SafeSQLAlchemyAsyncConfig:
    """Build the SQLAlchemy async database configuration."""
    if "sqlite" in settings.db.url:
        engine_config = EngineConfig(echo=settings.db.echo)
    else:
        engine_config = EngineConfig(
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.pool_overflow,
            pool_timeout=settings.db.pool_timeout,
            pool_pre_ping=settings.db.pool_pre_ping,
            echo=settings.db.echo,
        )

    return SafeSQLAlchemyAsyncConfig(
        connection_string=settings.db.url,
        metadata=Base.metadata,
        create_all=False,
        session_config=AsyncSessionConfig(expire_on_commit=False),
        engine_config=engine_config,
    )


def build_session_config(settings: Settings) -> CookieBackendConfig:
    """Build the client-side encrypted session configuration."""
    session_secret = hashlib.sha256(settings.secret_key.encode()).digest()
    return CookieBackendConfig(
        secret=session_secret,
        max_age=settings.session.max_age,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
        domain=settings.session.cookie_domain,
    )


def build_security_middleware(settings: Settings) -> list:
    """Build the security headers middleware list (empty if disabled)."""
    if not settings.security_headers.enabled:
        return []

    headers = settings.security_headers.build_headers(debug=settings.debug)
    csp_value = settings.security_headers.content_security_policy
    if not headers and not csp_value:
        return []

    return [
        DefineMiddleware(
            SecurityHeadersMiddleware,
            headers=headers,
            csp_value=csp_value,
            csp_nonce=settings.security_headers.csp_nonce,
            debug=settings.debug,
        )
    ]


def build_rate_limit_middleware(settings: Settings) -> list:
    """Build the rate limiting middleware list (empty if disabled)."""
    if not settings.rate_limit.enabled:
        return []

    return [
        DefineMiddleware(
            RateLimitMiddleware,
            requests_per_minute=settings.rate_limit.requests_per_minute,
            auth_requests_per_minute=settings.rate_limit.auth_requests_per_minute,
            paths=settings.rate_limit.paths,
        )
    ]


def build_template_config() -> TemplateConfig:
    """Build the Jinja template configuration with globals and filters."""
    working_dir_templates = Path(os.getcwd()) / "templates"
    template_dir = Path(__file__).parent / "templates"

    from skrift.forms import Form, csrf_field as _csrf_field

    def configure_template_engine(engine):
        engine.engine.globals.update({
            "now": datetime.now,
            "site_name": get_cached_site_name,
            "site_tagline": get_cached_site_tagline,
            "site_copyright_holder": get_cached_site_copyright_holder,
            "site_copyright_start_year": get_cached_site_copyright_start_year,
            "Form": Form,
            "csrf_field": _csrf_field,
            "csp_nonce": lambda: csp_nonce_var.get(""),
        })
        engine.engine.filters.update({"markdown": render_markdown})

    return TemplateConfig(
        directory=[working_dir_templates, template_dir],
        engine=JinjaTemplateEngine,
        engine_callback=configure_template_engine,
    )


def build_static_files_router():
    """Build the static files router with working-dir overrides."""
    working_dir_static = Path(os.getcwd()) / "static"
    return create_static_files_router(
        path="/static",
        directories=[working_dir_static, Path(__file__).parent / "static"],
    )
