import hashlib
import importlib
from datetime import datetime
from pathlib import Path

import yaml
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

from basesite.config import get_settings
from basesite.db.base import Base
from basesite.lib.exceptions import http_exception_handler, internal_server_error_handler


def load_controllers() -> list:
    """Load controllers from app.yaml configuration."""
    config_path = Path.cwd() / "app.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"app.yaml not found at {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    controllers = []
    for controller_spec in config.get("controllers", []):
        module_path, class_name = controller_spec.split(":")
        module = importlib.import_module(module_path)
        controller_class = getattr(module, class_name)
        controllers.append(controller_class)

    return controllers


def create_app() -> Litestar:
    """Create and configure the Litestar application."""
    settings = get_settings()

    # Load controllers from app.yaml
    controllers = load_controllers()

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
        route_handlers=[*controllers, static_files_router],
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
