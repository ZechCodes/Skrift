import hashlib
import importlib
from datetime import datetime
from pathlib import Path

import yaml
from advanced_alchemy.config import EngineConfig
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

from skrift.auth import sync_roles_to_database
from skrift.config import get_settings
from skrift.db.base import Base
from skrift.db.services.setting_service import (
    load_site_settings_cache,
    get_cached_site_name,
    get_cached_site_tagline,
    get_cached_site_copyright_holder,
    get_cached_site_copyright_start_year,
)
from skrift.lib.exceptions import http_exception_handler, internal_server_error_handler


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
    # Note: create_all is disabled - use migrations instead:
    #   skrift-db upgrade head
    # SQLite doesn't support connection pooling, so only set pool params for other DBs
    if "sqlite" in settings.db.url:
        engine_config = EngineConfig(echo=settings.db.echo)
    else:
        engine_config = EngineConfig(
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.pool_overflow,
            pool_timeout=settings.db.pool_timeout,
            echo=settings.db.echo,
        )

    db_config = SQLAlchemyAsyncConfig(
        connection_string=settings.db.url,
        metadata=Base.metadata,
        create_all=False,
        session_config=AsyncSessionConfig(expire_on_commit=False),
        engine_config=engine_config,
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
    # Site settings are loaded from the database cache via callable functions
    # so they're always current without requiring a restart
    template_dir = Path(__file__).parent.parent / "templates"
    template_config = TemplateConfig(
        directory=template_dir,
        engine=JinjaTemplateEngine,
        engine_callback=lambda engine: engine.engine.globals.update({
            "now": datetime.now,
            "site_name": get_cached_site_name,
            "site_tagline": get_cached_site_tagline,
            "site_copyright_holder": get_cached_site_copyright_holder,
            "site_copyright_start_year": get_cached_site_copyright_start_year,
        }),
    )

    # Static files
    static_files_router = create_static_files_router(
        path="/static",
        directories=[Path(__file__).parent.parent / "static"],
    )

    async def on_startup(_app: Litestar) -> None:
        """Sync roles and load site settings on application startup."""
        async with db_config.get_session() as session:
            await sync_roles_to_database(session)
            await load_site_settings_cache(session)

    app = Litestar(
        on_startup=[on_startup],
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
