import hashlib
from datetime import datetime
from pathlib import Path

from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.exceptions import HTTPException
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.static_files import create_static_files_router
from litestar.template import TemplateConfig

from app.config import get_settings
from app.controllers.auth import AuthController
from app.controllers.web import WebController
from app.db.base import Base
from app.lib.exceptions import http_exception_handler, internal_server_error_handler


def create_app() -> Litestar:
    """Create and configure the Litestar application."""
    settings = get_settings()

    # Database configuration
    db_config = SQLAlchemyAsyncConfig(
        connection_string=settings.database_url,
        metadata=Base.metadata,
        create_all=True,
        session_config=AsyncSessionConfig(expire_on_commit=False),
    )

    # Session configuration (client-side encrypted cookies)
    # Hash the secret key to ensure it's exactly 32 bytes (256-bit)
    session_secret = hashlib.sha256(settings.secret_key.encode()).digest()
    session_config = CookieBackendConfig(
        secret=session_secret,
        max_age=60 * 60 * 24 * 7,  # 7 days
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
    )

    # Template configuration
    template_dir = Path(__file__).parent.parent / "templates"
    template_config = TemplateConfig(
        directory=template_dir,
        engine=JinjaTemplateEngine,
        engine_callback=lambda engine: engine.engine.globals.update({"now": datetime.now}),
    )

    # Static files
    static_files_router = create_static_files_router(
        path="/static",
        directories=[Path(__file__).parent.parent / "static"],
    )

    app = Litestar(
        route_handlers=[AuthController, WebController, static_files_router],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        middleware=[session_config.middleware],
        template_config=template_config,
        compression_config=CompressionConfig(backend="gzip"),
        exception_handlers={
            HTTPException: http_exception_handler,
            Exception: internal_server_error_handler,
        },
        debug=settings.debug,
    )

    return app


app = create_app()
